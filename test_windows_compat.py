"""Dedicated Windows 10/11 compatibility tests covering paths, encodings, locks, processes, and media."""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tomllib
import zipfile
from zoneinfo import ZoneInfoNotFoundError

import pytest

from agent_policy import deduplicate_tool_text, reading_diagnostics
from codex_activity import (
    _ROLLOUT_COMPACT_CACHE,
    _ROLLOUT_PATH_CACHE,
    _ROLLOUT_STATE_CACHE,
    _count_rollout_compactions,
    _scan_rollout_tail,
)
from config import BridgeSettings, _read_dotenv_text
from configure_codex_context import _safe_backup_relpath, render_local_bridge_profile
from distribution import _is_loopback_peer, _normalize_peer_ip
from media_inputs import (
    FileStore,
    _convert_rtf_bytes_to_docx,
    _decode_bmp_or_ico_to_png_stdlib,
)
import platform_compat
from platform_compat import (
    atomic_write_json,
    resolve_timezone,
    sqlite_file_uri,
    subprocess_hidden_kwargs,
)
from responses_api import chat_packets
from schemas import ChatMessage
from tool_calling import ToolProtocolError, ToolStreamProcessor, parse_assistant_output
import tunnel_manager
from web_text import WebTextFilter, clean_web_text


def _tool_spec(name: str = "functions.exec_command") -> dict:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }


def _fenced_call(name: str, args: dict) -> str:
    ticks = "`" * 3
    payload = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    return f"{ticks}codex_tool_call\n{payload}\n{ticks}\n"


def test_timezone_fallback_when_iana_tzdata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_missing(_name: str):
        raise ZoneInfoNotFoundError("No time zone found with key Asia/Shanghai")

    monkeypatch.setattr(platform_compat, "ZoneInfo", _raise_missing)
    tz = resolve_timezone("Asia/Shanghai")
    from datetime import datetime

    dt = datetime(2026, 11, 16, 12, 0, tzinfo=tz)
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 8 * 3600


def test_atomic_write_json_retries_transient_windows_defender_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state" / "distribution.json"
    real_replace = os.replace
    attempts = {"count": 0}

    def _flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(5, "Access is denied (simulated Windows Defender scan)")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky_replace)
    atomic_write_json(target, {"client": "张三", "ok": True}, retries=5)
    assert attempts["count"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"client": "张三", "ok": True}
    assert not list(target.parent.glob(".*.tmp"))


def test_dotenv_handles_utf8_bom_and_gbk_chinese_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bom_env = tmp_path / "bom" / ".env"
    bom_env.parent.mkdir()
    bom_env.write_bytes("\ufeffBRIDGE_PORT=8021\r\nBRIDGE_DEFAULT_MODEL=gemini-4-carbon\r\n".encode("utf-8"))
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    monkeypatch.delenv("BRIDGE_DEFAULT_MODEL", raising=False)
    cfg = BridgeSettings.from_env(base_dir=bom_env.parent)
    assert cfg.port == 8021
    assert cfg.default_model == "gemini-4-carbon"

    gbk_env = tmp_path / "gbk" / ".env"
    gbk_env.parent.mkdir()
    gbk_env.write_bytes("# 本地网关端口设置（中文注释）\r\nBRIDGE_PORT=8022\r\n".encode("gbk"))
    decoded = _read_dotenv_text(gbk_env)
    assert "本地网关端口设置" in decoded
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    cfg_gbk = BridgeSettings.from_env(base_dir=gbk_env.parent)
    assert cfg_gbk.port == 8022


def test_sqlite_uri_formats_windows_drive_unc_spaces_and_cjk(tmp_path: Path) -> None:
    win_drive = sqlite_file_uri(r"C:\Users\王思涵\My Project\.codex\state_5.sqlite", "ro")
    assert win_drive.startswith("file:///C:/Users/")
    assert "\\" not in win_drive
    assert "%20" in win_drive
    assert win_drive.endswith("?mode=ro")

    win_unc = sqlite_file_uri(r"\\fileserver\shared folder\state_5.sqlite", "ro")
    assert win_unc.startswith("file://fileserver/shared%20folder/state_5.sqlite?mode=ro")

    cjk_dir = tmp_path / "中文 空格 目录"
    cjk_dir.mkdir()
    db_file = cjk_dir / "state_5.sqlite"
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()

    ro_con = sqlite3.connect(sqlite_file_uri(db_file, "ro"), uri=True)
    try:
        assert ro_con.execute("SELECT x FROM t").fetchone()[0] == 42
    finally:
        ro_con.close()


@pytest.mark.parametrize("chunk_size", [1, 3, 9, 64, 1024])
def test_windows_paths_linkify_across_stream_chunks_and_crlf_suggestions(chunk_size: int) -> None:
    ticks = "`" * 3
    sug_tag = "suggested" + "_questions"
    raw = (
        "已定位问题文件：\r\n"
        "- 主入口：`C:\\Users\\alice\\project\\server.py:128`\r\n"
        "- 含空格报告：`D:\\Work Space\\My Report (Final).md:15`\r\n"
        "- 局域网共享：`\\\\nas01\\team\\config.json`\r\n"
        "- 目录与正则保持原样：`C:\\Users\\alice\\project\\` 与 `C:\\d+`\r\n"
        f"{ticks}{sug_tag}\r\n"
        '{"questions": ["Next?"], "type": "followup"}\r\n'
        f"{ticks}\r\n"
    )
    expected = (
        "已定位问题文件：\r\n"
        "- 主入口：[server.py:128](C:/Users/alice/project/server.py:128)\r\n"
        "- 含空格报告：[My Report (Final).md:15](<D:/Work Space/My Report (Final).md:15>)\r\n"
        "- 局域网共享：[config.json](//nas01/team/config.json)\r\n"
        "- 目录与正则保持原样：`C:\\Users\\alice\\project\\` 与 `C:\\d+`"
    )
    assert clean_web_text(raw).strip() == expected

    flt = WebTextFilter()
    pieces = [flt.feed(raw[i : i + chunk_size]) for i in range(0, len(raw), chunk_size)]
    pieces.append(flt.finish())
    assert "".join(pieces).strip() == expected

    proc = ToolStreamProcessor([_tool_spec()])
    emitted = []
    for i in range(0, len(raw), chunk_size):
        emitted.extend(proc.feed(raw[i : i + chunk_size]))
    tail, parsed = proc.finalize()
    assert "".join(emitted + tail).strip() == expected
    assert (parsed.content or "").strip() == expected


def test_windows_parallel_read_commands_and_edit_conflict_detection() -> None:
    tools = [
        _tool_spec("functions.exec_command"),
        _tool_spec("functions.apply_patch"),
    ]
    parallel_reads = (
        "先并行读取 Windows 工作区信息。\r\n"
        + _fenced_call("functions.exec_command", {"cmd": r"C:\Tools\rg.exe -n BridgeSettings src\config.py 2>NUL"})
        + _fenced_call("functions.exec_command", {"cmd": r"Get-Content src\server.py | Select-Object -First 20"})
        + _fenced_call("functions.exec_command", {"cmd": r"git.exe -C C:\Repo status -s 2>$null"})
        + _fenced_call("functions.exec_command", {"cmd": "dir C:\\Repo\\"})
    )
    parsed = parse_assistant_output(parallel_reads, tools)
    assert len(parsed.tool_calls) == 4

    ticks = "`" * 3
    crlf_doc_with_call = (
        f"说明如下：\r\n{ticks}markdown\r\n[TOOL_CALL]\r\nlegacy example\r\n{ticks}\r\n"
        + _fenced_call("functions.exec_command", {"cmd": "py --version"})
    )
    assert len(parse_assistant_output(crlf_doc_with_call, tools).tool_calls) == 1

    patch_win_a = "*** Begin Patch\r\n*** Update File: C:\\Repo\\src\\alpha.py\r\n@@\r\n-x = 1\r\n+x = 2\r\n*** End Patch"
    patch_win_b = "*** Begin Patch\r\n*** Update File: C:\\Repo\\src\\beta.py\r\n@@\r\n-y = 1\r\n+y = 2\r\n*** End Patch"
    parsed_patches = parse_assistant_output(
        _fenced_call("functions.apply_patch", {"input": patch_win_a})
        + _fenced_call("functions.apply_patch", {"input": patch_win_b}),
        tools,
    )
    assert len(parsed_patches.tool_calls) == 2

    patch_rel_alias = "*** Begin Patch\r\n*** Update File: src\\ALPHA.py\r\n@@\r\n-x = 1\r\n+x = 3\r\n*** End Patch"
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(
            _fenced_call("functions.apply_patch", {"input": patch_win_a})
            + _fenced_call("functions.apply_patch", {"input": patch_rel_alias}),
            tools,
        )


def test_windows_read_diagnostics_and_crlf_deduplication() -> None:
    msgs = [ChatMessage(role="user", content="inspect")]
    body = "line content\r\n" * 450
    for idx in range(3):
        cid = f"call_{idx}"
        msgs.append(
            ChatMessage(
                role="assistant",
                tool_calls=[{
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": "functions.exec_command",
                        "arguments": json.dumps({"cmd": r"Get-Content C:\Repo\server.py"}),
                    },
                }],
            )
        )
        msgs.append(ChatMessage(role="tool", tool_call_id=cid, content=body))

    diag = reading_diagnostics(msgs)
    assert diag["repeated_read_calls"] == 2

    seen: dict = {}
    out1 = "Chunk ID: a1\r\nProcess exited with code 0\r\nOutput:\r\n" + body
    out2 = "Chunk ID: b2\r\nProcess exited with code 0\r\nOutput:\r\n" + body
    assert deduplicate_tool_text(out1, "c1", seen) == out1
    deduped = deduplicate_tool_text(out2, "c2", seen)
    assert "call_id=c1" in deduped and "Chunk ID: b2" in deduped


def test_codex_activity_binary_scan_preserves_offsets_on_crlf_jsonl(tmp_path: Path) -> None:
    _ROLLOUT_PATH_CACHE.clear()
    _ROLLOUT_STATE_CACHE.clear()
    _ROLLOUT_COMPACT_CACHE.clear()

    rollout = tmp_path / "rollout-crlf.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "compacted", "payload": {"window_number": 1, "message": "中文摘要 1"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "w1"}},
    ]
    rollout.write_bytes("".join(json.dumps(r, ensure_ascii=False) + "\r\n" for r in rows).encode("utf-8"))

    active, phase, tool_name, lifecycle = _scan_rollout_tail(str(rollout), chunk_size=37)
    assert (active, phase, tool_name, lifecycle) == (True, "running_tool", "exec_command", "task_started")
    assert _count_rollout_compactions(str(rollout), rollout.stat().st_size) == 1

    with rollout.open("ab") as fh:
        fh.write((json.dumps({"type": "compacted", "payload": {"window_number": 2}}, ensure_ascii=False) + "\r\n").encode("utf-8"))
    assert _count_rollout_compactions(str(rollout), rollout.stat().st_size) == 2


@pytest.mark.asyncio
async def test_chat_packets_handles_split_utf8_multibyte_and_crlf_sse() -> None:
    event_json = json.dumps({"choices": [{"delta": {"content": "你好 Windows 🚀"}}]}, ensure_ascii=False)
    raw_stream = f"data: {event_json}\r\n\r\ndata: [DONE]\r\n\r\n".encode("utf-8")

    async def _byte_by_byte():
        for i in range(0, len(raw_stream), 2):
            yield raw_stream[i : i + 2]

    got = [pkt async for pkt in chat_packets(_byte_by_byte())]
    assert len(got) == 1
    assert got[0]["choices"][0]["delta"]["content"] == "你好 Windows 🚀"


def test_stdlib_bmp_to_png_rtf_to_docx_and_portable_filestore_relocation(tmp_path: Path) -> None:
    px = b"\x10\x80\xf0" * 64
    bmp = struct.pack("<2sIHHI", b"BM", 54 + len(px), 0, 0, 54) + struct.pack(
        "<IiiHHIIiiII", 40, 8, 8, 1, 24, 0, len(px), 2835, 2835, 0, 0
    ) + px
    png = _decode_bmp_or_ico_to_png_stdlib("image/bmp", bmp)
    assert png is not None and png.startswith(b"\x89PNG\r\n\x1a\n")

    rtf = (
        b"{\\rtf1\\ansi\\ansicpg936\\uc1 "
        b"{\\fonttbl{\\f0 SimSun;}}"
        b"Windows \\u20860?\\u23481?\\par Second Line}"
    )
    docx = _convert_rtf_bytes_to_docx(rtf)
    with zipfile.ZipFile(io.BytesIO(docx)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
        assert "Windows 兼容" in xml
        assert "Second Line" in xml

    loc_a = tmp_path / "drive_c" / "generated"
    store_a = FileStore(loc_a)
    saved = store_a.save(b"hello portable world", filename="notes.txt", declared_mime="text/plain")
    fid = saved["id"]

    loc_b = tmp_path / "drive_d" / "relocated_generated"
    shutil.move(str(loc_a), str(loc_b))
    store_b = FileStore(loc_b)
    meta_b, data_b = store_b.read_bytes(fid)
    assert data_b == b"hello portable world"
    assert Path(meta_b["path"]).resolve().is_relative_to(loc_b.resolve())


def test_windows_tunnel_process_and_loopback_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _normalize_peer_ip("::1%14") == "::1"
    assert _is_loopback_peer("127.0.0.1")
    assert _is_loopback_peer("127.0.1.5")
    assert _is_loopback_peer("::1")
    assert not _is_loopback_peer("192.168.1.20")

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(tunnel_manager, "IS_WINDOWS", True)
    kw = subprocess_hidden_kwargs(detached=True)
    assert "creationflags" in kw and kw["creationflags"] != 0
    assert "start_new_session" not in kw

    monkeypatch.setattr(tunnel_manager, "find_cloudflared_binary", lambda: None)
    tm = tunnel_manager.PublicTunnelManager(target_port=8099, state_path=tmp_path / "tunnel.json")
    with pytest.raises(RuntimeError, match="winget install --id Cloudflare.cloudflared"):
        asyncio.run(tm.start())


def test_configure_codex_context_windows_paths_and_backup_relpath(tmp_path: Path) -> None:
    rel = _safe_backup_relpath(tmp_path / "sub" / "config.toml")
    assert not rel.is_absolute()
    joined = (tmp_path / "backups") / rel
    assert joined.resolve().is_relative_to((tmp_path / "backups").resolve())

    rendered = render_local_bridge_profile(
        Path(r"C:\Users\alice\Gemini-Enterprise-Bridge"),
        r"C:\Users\alice\AppData\Local\Gemini-Enterprise-Bridge\venv\Scripts\python.exe",
        port=8000,
    )
    parsed = tomllib.loads(rendered)
    assert parsed["model"] == "gemini-4-carbon"
    assert parsed["mcp_servers"]["gemini_bridge_images"]["command"].endswith("python.exe")

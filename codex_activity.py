"""Read-only Codex local session lifecycle & sub-phase detector for worker slots.

Modeled after CodeIsland's authoritative Codex state resolution (`state_5.sqlite` +
backward chunk scanning of `rollout-*.jsonl`), enriched with tool-execution vs.
model-waiting sub-phase detection so Bridge worker seats never flicker to idle
between multi-step Codex turns.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional, Tuple

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_TERMINAL_EVENTS = frozenset({
    "task_complete",
    "turn_aborted",
    "turn_failed",
    "shutdown_complete",
})

_CACHE_LOCK = RLock()
# (codex_root_str, session_uuid) -> (checked_monotonic, rollout_path_str)
_ROLLOUT_PATH_CACHE: Dict[Tuple[str, str], Tuple[float, str]] = {}
# rollout_path_str -> (mtime_ns, file_size, raw_active, phase, tool_name, last_lifecycle)
_ROLLOUT_STATE_CACHE: Dict[str, Tuple[int, int, bool, str, str, str]] = {}
# rollout_path_str -> (scanned_offset, cnt_top, max_window_number, cnt_event)
_ROLLOUT_COMPACT_CACHE: Dict[str, Tuple[int, int, int, int]] = {}
# codeisland_sessions_json_path -> (mtime_ns, file_size, {sid: transcript_path})
_CODEISLAND_MAP_CACHE: Dict[str, Tuple[int, int, Dict[str, str]]] = {}


def normalize_codex_session_id(session_id: Optional[str]) -> str:
    """Extract a clean UUID from a raw or `codexapp:`-prefixed session identifier."""
    raw = (session_id or "").strip()
    if raw.lower().startswith("codexapp:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw or not _UUID_RE.match(raw):
        return ""
    return raw


def _lookup_path_in_codeisland(sid: str, codeisland_dir: Path) -> str:
    """Optional read-only fast path via `~/.codeisland/sessions.json` if present."""
    sessions_file = codeisland_dir / "sessions.json"
    try:
        if not sessions_file.is_file():
            return ""
        st = sessions_file.stat()
        key = str(sessions_file)
        with _CACHE_LOCK:
            cached = _CODEISLAND_MAP_CACHE.get(key)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                return cached[2].get(sid, "")
        entries = json.loads(sessions_file.read_text(encoding="utf-8", errors="ignore"))
        mapping: Dict[str, str] = {}
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                tpath = str(item.get("transcriptPath") or "").strip()
                if not tpath:
                    continue
                for candidate in (item.get("providerSessionId"), item.get("sessionId")):
                    norm = normalize_codex_session_id(str(candidate or ""))
                    if norm:
                        mapping[norm] = tpath
        with _CACHE_LOCK:
            _CODEISLAND_MAP_CACHE[key] = (st.st_mtime_ns, st.st_size, mapping)
        return mapping.get(sid, "")
    except Exception:
        return ""


def _resolve_rollout_path(
    sid: str,
    codex_home: Path,
    codeisland_dir: Optional[Path] = None,
) -> str:
    """Locate the rollout `.jsonl` file for a Codex thread UUID."""
    now_mono = time.monotonic()
    cache_key = (str(codex_home), sid)
    with _CACHE_LOCK:
        cached = _ROLLOUT_PATH_CACHE.get(cache_key)
        if cached is not None:
            checked_at, cached_path = cached
            if cached_path and (now_mono - checked_at) < 30.0 and os.path.isfile(cached_path):
                return cached_path
            if not cached_path and (now_mono - checked_at) < 2.0:
                return ""

    resolved = ""
    # 1) Authoritative lookup in `~/.codex/state_5.sqlite`
    db_path = codex_home / "state_5.sqlite"
    try:
        if db_path.is_file():
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.05)
            try:
                row = con.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                if row and row[0]:
                    candidate = str(row[0]).strip()
                    if candidate and os.path.isfile(candidate):
                        resolved = candidate
            finally:
                con.close()
    except Exception:
        resolved = ""

    # 2) Optional fallback via CodeIsland's live session registry when using default home
    if not resolved:
        default_home = Path.home() / ".codex"
        ci_root = codeisland_dir
        if ci_root is None and codex_home == default_home:
            ci_root = Path.home() / ".codeisland"
        if ci_root is not None:
            ci_candidate = _lookup_path_in_codeisland(sid, ci_root)
            if ci_candidate and os.path.isfile(ci_candidate):
                resolved = ci_candidate

    # 3) Direct filesystem scan of recent `~/.codex/sessions/YYYY/MM/DD/*<sid>*.jsonl`
    if not resolved:
        sessions_root = codex_home / "sessions"
        try:
            if sessions_root.is_dir():
                day_dirs = []
                for year_dir in sorted(sessions_root.iterdir(), reverse=True)[:2]:
                    if not year_dir.is_dir():
                        continue
                    for month_dir in sorted(year_dir.iterdir(), reverse=True)[:2]:
                        if not month_dir.is_dir():
                            continue
                        for day_dir in sorted(month_dir.iterdir(), reverse=True)[:3]:
                            if day_dir.is_dir():
                                day_dirs.append(day_dir)
                for ddir in day_dirs[:4]:
                    matches = sorted(ddir.glob(f"*{sid}*.jsonl"), reverse=True)
                    if matches:
                        resolved = str(matches[0])
                        break
        except Exception:
            pass

    with _CACHE_LOCK:
        _ROLLOUT_PATH_CACHE[cache_key] = (now_mono, resolved)
    return resolved


def _scan_rollout_tail(
    path: str,
    chunk_size: int = 131072,
    max_scan_bytes: int = 16 * 1024 * 1024,
    max_line_bytes: int = 2 * 1024 * 1024,
) -> Tuple[bool, str, str, str]:
    """Walk backwards from EOF to find the latest Codex turn lifecycle & sub-phase.

    Returns `(raw_active, phase, tool_name, last_lifecycle)`.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return (False, "idle", "", "")

    try:
        file_size = os.lseek(fd, 0, os.SEEK_END)
        if file_size <= 0:
            return (False, "idle", "", "")
        lower_bound = max(0, file_size - max_scan_bytes)
        cursor = file_size
        leading_fragment = b""
        discarding_oversized = False

        seen_output_call_ids: set[str] = set()
        saw_turn_message = False
        active_tool_name = ""
        saw_waiting_signal = False

        while cursor > lower_bound:
            read_size = min(cursor - lower_bound, chunk_size)
            cursor -= read_size
            os.lseek(fd, cursor, os.SEEK_SET)
            chunk = os.read(fd, read_size)
            if not chunk:
                break

            if discarding_oversized:
                nl_idx = chunk.rfind(b"\n")
                if nl_idx < 0:
                    continue
                combined = chunk[: nl_idx + 1]
                discarding_oversized = False
            else:
                combined = chunk + leading_fragment

            parts = combined.split(b"\n")
            if cursor > lower_bound and parts:
                first_part = parts.pop(0)
                if len(first_part) > max_line_bytes:
                    leading_fragment = b""
                    discarding_oversized = True
                else:
                    leading_fragment = first_part
            else:
                leading_fragment = b""
                if lower_bound > 0 and parts:
                    parts.pop(0)

            for raw_line in reversed(parts):
                if not raw_line or len(raw_line) > max_line_bytes:
                    continue
                probe = raw_line[:360]
                if b'"event_msg"' not in probe and b'"response_item"' not in probe:
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                rtype = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue

                if rtype == "event_msg":
                    ev_type = str(payload.get("type") or "").strip()
                    if ev_type in _TERMINAL_EVENTS:
                        return (False, "idle", "", ev_type)
                    if ev_type == "task_started":
                        if active_tool_name:
                            return (True, "running_tool", active_tool_name, "task_started")
                        return (True, "waiting_model", "", "task_started")
                    if ev_type in ("user_message", "context_compacted", "compacted"):
                        saw_waiting_signal = True

                elif rtype == "response_item":
                    itype = str(payload.get("type") or "").strip()
                    if itype in ("function_call_output", "custom_tool_call_output"):
                        cid = str(payload.get("call_id") or "").strip()
                        if cid:
                            seen_output_call_ids.add(cid)
                        saw_waiting_signal = True
                    elif itype in ("function_call", "custom_tool_call"):
                        cid = str(payload.get("call_id") or "").strip()
                        tname = str(payload.get("name") or itype).strip()
                        if not saw_turn_message and (not cid or cid not in seen_output_call_ids):
                            if not active_tool_name:
                                active_tool_name = tname
                        else:
                            saw_waiting_signal = True
                    elif itype == "web_search_call":
                        if not saw_turn_message and not saw_waiting_signal and not active_tool_name:
                            active_tool_name = "web_search"
                    elif itype in ("message", "reasoning", "agent_message"):
                        saw_turn_message = True
                        saw_waiting_signal = True

        return (False, "idle", "", "")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _count_rollout_compactions(path: str, file_size: int) -> int:
    """Incrementally count compaction checkpoints in an append-only Codex rollout JSONL."""
    if file_size <= 0:
        return 0
    with _CACHE_LOCK:
        cached = _ROLLOUT_COMPACT_CACHE.get(path)
    if cached is not None and cached[0] == file_size:
        _, cnt_top, max_win, cnt_ev = cached
        return max(cnt_top, max_win) if (cnt_top or max_win) else cnt_ev

    if cached is not None and 0 <= cached[0] < file_size:
        offset, cnt_top, max_win, cnt_ev = cached
    else:
        offset, cnt_top, max_win, cnt_ev = (0, 0, 0, 0)

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return max(cnt_top, max_win) if (cnt_top or max_win) else cnt_ev

    try:
        os.lseek(fd, offset, os.SEEK_SET)
        carry = b""
        carry_start = offset
        cursor = offset
        while cursor < file_size:
            chunk = os.read(fd, min(524288, file_size - cursor))
            if not chunk:
                break
            cursor += len(chunk)
            buf = carry + chunk
            nl = buf.rfind(b"\n")
            if nl < 0:
                carry = buf
                continue
            process_part = buf[:nl]
            carry = buf[nl + 1 :]
            carry_start = cursor - len(carry)
            if b'"compacted"' not in process_part and b'"context_compacted"' not in process_part:
                continue
            for raw_line in process_part.split(b"\n"):
                head = raw_line[:260]
                if b'"compacted"' not in head and b'"context_compacted"' not in head:
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                rt = obj.get("type")
                if rt == "compacted":
                    cnt_top += 1
                    pl = obj.get("payload")
                    if isinstance(pl, dict) and isinstance(pl.get("window_number"), int):
                        max_win = max(max_win, pl["window_number"])
                elif rt == "event_msg":
                    pl = obj.get("payload")
                    if isinstance(pl, dict) and pl.get("type") in ("context_compacted", "compacted"):
                        cnt_ev += 1
        with _CACHE_LOCK:
            _ROLLOUT_COMPACT_CACHE[path] = (carry_start, cnt_top, max_win, cnt_ev)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return max(cnt_top, max_win) if (cnt_top or max_win) else cnt_ev


def lookup_codex_thread_activity(
    session_id: Optional[str],
    codex_home: Optional[Path] = None,
    codeisland_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
    max_idle_age_seconds: float = 900.0,
) -> Dict[str, Any]:
    """Inspect local Codex state for `session_id` with mtime/size caching."""
    sid = normalize_codex_session_id(session_id)
    if not sid:
        return {
            "known": False,
            "active": False,
            "phase": "idle",
            "tool_name": "",
            "last_lifecycle": "",
            "updated_at": 0,
            "stale": False,
            "compaction_count": 0,
        }

    root = codex_home or (Path.home() / ".codex")
    rollout_path = _resolve_rollout_path(sid, codex_home=root, codeisland_dir=codeisland_dir)
    if not rollout_path:
        return {
            "known": False,
            "active": False,
            "phase": "idle",
            "tool_name": "",
            "last_lifecycle": "",
            "updated_at": 0,
            "stale": False,
            "compaction_count": 0,
        }

    try:
        st = os.stat(rollout_path)
    except OSError:
        return {
            "known": False,
            "active": False,
            "phase": "idle",
            "tool_name": "",
            "last_lifecycle": "",
            "updated_at": 0,
            "stale": False,
            "compaction_count": 0,
        }

    with _CACHE_LOCK:
        cached = _ROLLOUT_STATE_CACHE.get(rollout_path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            _, _, raw_active, raw_phase, tool_name, lifecycle = cached
        else:
            cached = None

    if cached is None:
        raw_active, raw_phase, tool_name, lifecycle = _scan_rollout_tail(rollout_path)
        with _CACHE_LOCK:
            _ROLLOUT_STATE_CACHE[rollout_path] = (
                st.st_mtime_ns,
                st.st_size,
                raw_active,
                raw_phase,
                tool_name,
                lifecycle,
            )

    now_val = time.time() if now_ts is None else float(now_ts)
    file_mtime = st.st_mtime_ns / 1_000_000_000.0
    age_sec = max(0.0, now_val - file_mtime)
    is_stale = bool(raw_active and max_idle_age_seconds > 0 and age_sec > max_idle_age_seconds)
    effective_active = bool(raw_active and not is_stale)
    effective_phase = raw_phase if effective_active else "idle"
    compaction_count = _count_rollout_compactions(rollout_path, st.st_size)

    return {
        "known": True,
        "active": effective_active,
        "phase": effective_phase,
        "tool_name": tool_name if effective_active else "",
        "last_lifecycle": lifecycle,
        "updated_at": int(file_mtime),
        "stale": is_stale,
        "compaction_count": compaction_count,
    }


def resolve_slot_activity(
    ws: Dict[str, Any],
    fallback_session_id: Optional[str] = None,
    codex_home: Optional[Path] = None,
    codeisland_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
    max_idle_age_seconds: float = 900.0,
) -> Dict[str, Any]:
    """Combine Bridge in-flight HTTP status with local Codex session lifecycle."""
    now_val = time.time() if now_ts is None else float(now_ts)
    http_busy = bool(ws.get("busy"))
    waiting_count = int(ws.get("waiting_count") or 0)
    pending_tool_until = float(ws.get("pending_tool_call_until") or 0.0)
    pinned_sid = str(ws.get("pinned_session_id") or fallback_session_id or "").strip()

    codex_info = lookup_codex_thread_activity(
        pinned_sid,
        codex_home=codex_home,
        codeisland_dir=codeisland_dir,
        now_ts=now_val,
        max_idle_age_seconds=max_idle_age_seconds,
    )

    if http_busy:
        return {
            "session_active": True,
            "activity_phase": "generating",
            "activity_label": "工作中 · 模型回复",
            "activity_source": "bridge_http",
            "tool_name": "",
            "codex_updated_at": codex_info["updated_at"],
            "compaction_count": codex_info["compaction_count"],
        }

    if waiting_count > 0:
        return {
            "session_active": True,
            "activity_phase": "queued",
            "activity_label": f"工作中 · 排队 {waiting_count}",
            "activity_source": "bridge_queue",
            "tool_name": "",
            "codex_updated_at": codex_info["updated_at"],
            "compaction_count": codex_info["compaction_count"],
        }

    if codex_info["known"]:
        if codex_info["active"]:
            if codex_info["phase"] == "running_tool":
                return {
                    "session_active": True,
                    "activity_phase": "running_tool",
                    "activity_label": "工作中 · 执行命令",
                    "activity_source": "codex_rollout",
                    "tool_name": codex_info["tool_name"],
                    "codex_updated_at": codex_info["updated_at"],
                    "compaction_count": codex_info["compaction_count"],
                }
            return {
                "session_active": True,
                "activity_phase": "waiting_model",
                "activity_label": "工作中 · 等待回复",
                "activity_source": "codex_rollout",
                "tool_name": "",
                "codex_updated_at": codex_info["updated_at"],
                "compaction_count": codex_info["compaction_count"],
            }
        return {
            "session_active": False,
            "activity_phase": "idle",
            "activity_label": "空闲待命",
            "activity_source": "codex_rollout",
            "tool_name": "",
            "codex_updated_at": codex_info["updated_at"],
            "compaction_count": codex_info["compaction_count"],
        }

    if pending_tool_until > now_val:
        return {
            "session_active": True,
            "activity_phase": "running_tool",
            "activity_label": "工作中 · 执行命令",
            "activity_source": "pending_tool_window",
            "tool_name": "",
            "codex_updated_at": 0,
            "compaction_count": 0,
        }

    return {
        "session_active": False,
        "activity_phase": "idle",
        "activity_label": "空闲待命",
        "activity_source": "bridge_idle",
        "tool_name": "",
        "codex_updated_at": 0,
        "compaction_count": 0,
    }

"""End-to-end unit and wire tests for multimodal file, audio, video, and image inputs/outputs."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import struct
import subprocess
import wave
import zipfile

from fastapi.testclient import TestClient
import pytest

from config import BridgeSettings
from extension_exporter import render_spark_icon_png
from imagegen_mcp import handle as mcp_handle
import media_inputs
from media_inputs import (
    _POSIX_FFMPEG_PATHS,
    _find_binary,
    FileStore,
    InvalidAttachmentError,
    resolve_mime_and_name,
)
from platform_compat import subprocess_hidden_kwargs
from server import create_app
from test_responses import browser, packets


def _make_wav_bytes(duration_sec: float = 0.25, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x10\x00" * int(sample_rate * duration_sec))
    return buf.getvalue()


def _make_pdf_bytes(text: str = "TEST-PDF-TOKEN") -> bytes:
    stream = f"BT /F1 18 Tf 50 700 Td ({text}) Tj ET".encode("ascii")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = [0]
    for o in objs:
        offs.append(len(out))
        out.extend(o)
    xref = len(out)
    out.extend(f"xref\n0 {len(offs)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offs[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(offs)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def _make_openxml_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document>DOCX-OK</w:document>")
    return buf.getvalue()


def _make_bmp_bytes(width: int = 8, height: int = 8) -> bytes:
    stride = ((width * 3 + 3) // 4) * 4
    pixel_bytes = (b"\xeb\x63\x25" * width + b"\x00" * (stride - width * 3)) * height
    file_size = 54 + len(pixel_bytes)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixel_bytes), 2835, 2835, 0, 0)
    return header + dib + pixel_bytes


def _make_mp4_bytes(tmp_path: Path) -> bytes:
    ffmpeg = _find_binary("ffmpeg", _POSIX_FFMPEG_PATHS)
    if ffmpeg:
        png = tmp_path / "f.png"
        mp4 = tmp_path / "v.mp4"
        png.write_bytes(render_spark_icon_png(32))
        proc = subprocess.run(
            [
                ffmpeg, "-y", "-loop", "1", "-i", str(png),
                "-t", "0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4),
            ],
            check=False,
            capture_output=True,
            **subprocess_hidden_kwargs(),
        )
        if proc.returncode == 0 and mp4.is_file() and mp4.stat().st_size > 0:
            return mp4.read_bytes()
    ftyp = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 512, b"isom", b"mp41")
    moov = struct.pack(">I4s", 8, b"moov")
    mdat = struct.pack(">I4s8s", 16, b"mdat", b"\x00" * 8)
    return ftyp + moov + mdat


def test_format_validation_and_web_normalization(tmp_path: Path) -> None:
    # BMP normalizes to image/png on every platform (Pillow / sips / stdlib fallback)
    bmp_src = tmp_path / "src.bmp"
    bmp_src.write_bytes(_make_bmp_bytes(12, 12))
    img_targets = [bmp_src]

    sips = _find_binary("sips", ("/usr/bin/sips",))
    if sips:
        png_src = tmp_path / "src.png"
        heic_src = tmp_path / "src.heic"
        png_src.write_bytes(render_spark_icon_png(32))
        subprocess.run([sips, "-s", "format", "heic", str(png_src), "--out", str(heic_src)], check=True, capture_output=True)
        img_targets.append(heic_src)

    for img_file in img_targets:
        mime, cat, norm_raw, safe_name = resolve_mime_and_name(img_file.read_bytes(), filename=img_file.name)
        assert mime == "image/png"
        assert cat == "image"
        assert norm_raw.startswith(b"\x89PNG\r\n\x1a\n")
        assert safe_name.endswith(".png")

    # WAV normalizes to audio/mp4 (.m4a) when host audio transcoder is available, and stays audio/wav when False
    wav_raw = _make_wav_bytes()
    mime_w, cat_w, m4a_raw, name_w = resolve_mime_and_name(wav_raw, filename="clip.wav", normalize_for_web=True)
    assert cat_w == "audio"
    if mime_w == "audio/mp4":
        assert name_w == "clip.m4a"
        assert b"ftyp" in m4a_raw[:16]
    else:
        assert mime_w == "audio/wav"
        assert name_w == "clip.wav"

    mime_raw_wav, _, kept_wav, name_raw_wav = resolve_mime_and_name(wav_raw, filename="clip.wav", normalize_for_web=False)
    assert mime_raw_wav == "audio/wav"
    assert kept_wav == wav_raw
    assert name_raw_wav == "clip.wav"

    # RTF normalizes to DOCX across all platforms (textutil or built-in OpenXML converter)
    rtf_raw = b"{\\rtf1\\ansi\\deff0 Hello RTF World}"
    mime_d, cat_d, docx_raw, name_d = resolve_mime_and_name(rtf_raw, filename="note.rtf")
    assert mime_d == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert cat_d == "document"
    assert name_d == "note.docx"
    assert docx_raw.startswith(b"PK\x03\x04")

    # MOV normalizes to video/mp4 via ffmpeg when available
    ffmpeg = _find_binary("ffmpeg", _POSIX_FFMPEG_PATHS)
    if ffmpeg:
        _make_mp4_bytes(tmp_path)
        mov_path = tmp_path / "clip.mov"
        subprocess.run(
            [ffmpeg, "-y", "-i", str(tmp_path / "v.mp4"), "-c", "copy", str(mov_path)],
            check=True,
            capture_output=True,
            **subprocess_hidden_kwargs(),
        )
        mime_v, cat_v, norm_mp4, name_v = resolve_mime_and_name(mov_path.read_bytes(), filename="clip.mov")
        assert mime_v == "video/mp4"
        assert cat_v == "video"
        assert name_v == "clip.mp4"
        assert len(norm_mp4) > 0

    # Mismatched header is rejected
    with pytest.raises(InvalidAttachmentError):
        resolve_mime_and_name(b"not a pdf", declared_mime="application/pdf", filename="fake.pdf")


def test_v1_files_multipart_and_json_crud(tmp_path: Path) -> None:
    app = create_app(BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path)))
    pdf_raw = _make_pdf_bytes("CRUD-991")
    with TestClient(app) as client:
        # Multipart upload
        res = client.post(
            "/v1/files",
            files={"file": ("report.pdf", pdf_raw, "application/pdf")},
            data={"purpose": "user_data"},
        )
        assert res.status_code == 200
        meta = res.json()
        fid = meta["id"]
        assert meta["filename"] == "report.pdf"
        assert meta["mime_type"] == "application/pdf"

        # List, retrieve, download content
        assert any(f["id"] == fid for f in client.get("/v1/files").json()["data"])
        assert client.get(f"/v1/files/{fid}").json()["bytes"] == len(pdf_raw)
        assert client.get(f"/v1/files/{fid}/content").content == pdf_raw

        # JSON upload (DOCX, XLSX, PPTX, CSV)
        openxml = _make_openxml_bytes()
        for ext in ("docx", "xlsx", "pptx"):
            r2 = client.post("/v1/files", json={
                "filename": f"sample.{ext}",
                "file_data": base64.b64encode(openxml).decode("ascii"),
            })
            assert r2.status_code == 200
            assert r2.json()["category"] == "document"

        # Delete
        assert client.delete(f"/v1/files/{fid}").json()["deleted"] is True
        assert client.get(f"/v1/files/{fid}").status_code == 404


def _has_audio_transcoder() -> bool:
    return bool(_find_binary("ffmpeg") or _find_binary("afconvert", ("/usr/bin/afconvert",)))


def test_responses_and_chat_multimodal_parts_and_large_staging(tmp_path: Path) -> None:
    pdf_raw = _make_pdf_bytes("DOC-42")
    wav_raw = _make_wav_bytes()
    mp4_raw = _make_mp4_bytes(tmp_path)
    big_csv = ("col1,col2\n" + ("alpha,1234567890\n" * 95000)).encode("utf-8")  # > 1.5 MB -> staged via fetch_url!
    expected_audio_mime = "audio/mp4" if _has_audio_transcoder() else "audio/wav"

    with browser(["Read PDF, audio, video, and large CSV."], generated_dir=str(tmp_path)) as (client, commands):
        up_csv = client.post("/v1/files", json={
            "filename": "huge.csv",
            "file_data": base64.b64encode(big_csv).decode("ascii"),
        }).json()

        resp = client.post("/v1/responses", json={
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Summarize all four attachments."},
                    {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(pdf_raw).decode()},
                    {"type": "input_audio", "format": "wav", "data": base64.b64encode(wav_raw).decode()},
                    {"type": "input_video", "filename": "demo.mp4", "file_data": "data:video/mp4;base64," + base64.b64encode(mp4_raw).decode()},
                    {"type": "input_file", "file_id": up_csv["id"]},
                ],
            }],
        })
        assert resp.status_code == 200
        atts = commands[0]["attachments"]
        assert len(atts) == 4
        by_cat = {a["category"]: a for a in atts if a["name"] != "huge.csv"}
        assert by_cat["document"]["name"] == "brief.pdf"
        assert by_cat["audio"]["mime_type"] == expected_audio_mime
        assert by_cat["video"]["mime_type"] == "video/mp4"

        huge_att = next(a for a in atts if a["name"] == "huge.csv")
        assert "data_url" not in huge_att
        assert huge_att["fetch_url"].endswith("/api/bridge/attachments/" + up_csv["id"])
        staged_path = "/api/bridge/attachments/" + up_csv["id"]
        assert client.get(staged_path).content == big_csv


def test_multimodal_outputs_image_edits_video_gen_and_audio_transcription(tmp_path: Path) -> None:
    png_raw = render_spark_icon_png(32)
    png_url = "data:image/png;base64," + base64.b64encode(png_raw).decode()
    mp4_raw = _make_mp4_bytes(tmp_path)
    mp4_url = "data:video/mp4;base64," + base64.b64encode(mp4_raw).decode()
    pdf_raw = _make_pdf_bytes("OUT-PDF")
    pdf_url = "data:application/pdf;base64," + base64.b64encode(pdf_raw).decode()
    expected_voice_name = "voice.m4a" if _has_audio_transcoder() else "voice.wav"

    replies = [
        {"type": "request_complete", "reply": "", "output_images": [{"data_url": png_url}]},
        {"type": "request_complete", "reply": "", "output_files": [{"data_url": mp4_url, "mime_type": "video/mp4", "name": "ocean.mp4"}]},
        "The verification code is 7492.",
        {"type": "request_complete", "reply": "Here is your exported report.", "output_files": [{"data_url": pdf_url, "mime_type": "application/pdf", "name": "out.pdf"}]},
    ]
    with browser(replies, generated_dir=str(tmp_path)) as (client, commands):
        # 1. /v1/images/edits (multipart)
        ed = client.post(
            "/v1/images/edits",
            files={"image": ("in.png", png_raw, "image/png")},
            data={"prompt": "Make it gold", "response_format": "b64_json"},
        )
        assert ed.status_code == 200
        assert commands[0]["image_generation"] is True
        assert len(commands[0]["attachments"]) == 1
        assert base64.b64decode(ed.json()["data"][0]["b64_json"]) == png_raw

        # 2. /v1/videos/generations
        vid = client.post("/v1/videos/generations", json={"prompt": "Ocean wave", "response_format": "url"})
        assert vid.status_code == 200
        assert commands[1]["video_generation"] is True
        vdata = vid.json()["data"][0]
        assert Path(vdata["path"]).read_bytes() == mp4_raw
        dl_path = "/v1/files/" + vdata["file_id"] + "/content"
        assert client.get(dl_path).content == mp4_raw

        # 3. /v1/audio/transcriptions (multipart WAV -> auto-converted M4A when transcoder available)
        tr = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("voice.wav", _make_wav_bytes(), "audio/wav")},
            data={"response_format": "json"},
        )
        assert tr.status_code == 200
        assert tr.json()["text"] == "The verification code is 7492."
        assert commands[2]["attachments"][0]["name"] == expected_voice_name

        # 4. Streaming /v1/responses with generated downloadable PDF attachment
        evts = packets(client.post("/v1/responses", json={"input": "Export report", "stream": True}).text)
        assert evts[-1]["type"] == "response.completed"
        msg_out = evts[-1]["response"]["output"][0]["content"][0]
        assert "Generated File: out.pdf" in msg_out["text"]
        assert msg_out["annotations"][0]["filename"] == "out.pdf"


def test_mcp_tool_list_includes_multimodal_tools() -> None:
    res = mcp_handle({"method": "tools/list"})
    names = {t["name"] for t in res["tools"]}
    assert {"generate_image", "generate_video", "analyze_media"} <= names


def test_attachment_only_turn_and_nested_tool_result_preserve_attachments(tmp_path: Path) -> None:
    png_raw = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    png_url = "data:image/png;base64," + base64.b64encode(png_raw).decode()
    txt_url = "data:text/plain;base64," + base64.b64encode(b"standalone file body").decode()

    with browser(["Described attachment.", "Read tool image."], generated_dir=str(tmp_path)) as (client, commands):
        # 1. Standalone attachment without accompanying text must still dispatch attachment
        res1 = client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4-carbon",
                "messages": [
                    {"role": "user", "content": [{"type": "file", "file": {"filename": "only.txt", "file_data": txt_url}}]}
                ],
            },
        )
        assert res1.status_code == 200
        assert len(commands[0]["attachments"]) == 1
        assert commands[0]["attachments"][0]["name"] == "only.txt"
        assert commands[0]["attachments"][0]["data_url"].startswith("data:text/plain;base64,")

        # 2. Nested tool_result multimodal blocks must be extracted and staged
        res2 = client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-4-carbon",
                "messages": [
                    {"role": "user", "content": "Inspect screenshot"},
                    {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "view", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": [{"type": "image_url", "image_url": {"url": png_url}}]}]},
                ],
            },
        )
        assert res2.status_code == 200
        assert len(commands[1]["attachments"]) == 1
        assert commands[1]["attachments"][0]["mime_type"] == "image/png"


def test_same_semver_outdated_driver_version_triggers_extension_reload() -> None:
    import asyncio
    from bridge_hub import BrowserBridgeHub
    from version import APP_VERSION

    async def _check() -> None:
        hub = BrowserBridgeHub(backend_version=APP_VERSION)
        hub.register_http_client("ext-test", tab_id=1)
        reply = await hub.handle_browser_event(
            "ext-test",
            {"type": "tab_status", "extension_version": APP_VERSION, "driver_version": "2.4.0", "tab_id": 1, "ready": True},
        )
        assert reply is not None
        assert reply.get("_emit_extension_reload") == APP_VERSION

    asyncio.run(_check())


def test_wav_fallback_when_no_audio_transcoder_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_inputs, "_find_binary", lambda *a, **kw: None)
    monkeypatch.setattr("test_multimodal._find_binary", lambda *a, **kw: None)

    r_dir = tmp_path / "responses"
    r_dir.mkdir()
    test_responses_and_chat_multimodal_parts_and_large_staging(r_dir)

    m_dir = tmp_path / "outputs"
    m_dir.mkdir()
    test_multimodal_outputs_image_edits_video_gen_and_audio_transcription(m_dir)

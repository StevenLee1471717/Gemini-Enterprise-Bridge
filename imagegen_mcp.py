#!/usr/bin/env python3
"""Dependency-free stdio MCP multimodal & media generation tools for the Codex bridge profile."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

from media_inputs import SUPPORTED_MIME_SPECS, decode_base64_payload, resolve_mime_and_name

BASE_URL = "http://127.0.0.1:8000"

TOOLS = [
    {
        "name": "generate_image",
        "description": (
            "Generate a real image using Gemini Enterprise, or edit/combine reference image(s) "
            "by supplying image_path or image_paths (supports PNG, JPEG, WebP, GIF, HEIC, HEIF, BMP, TIFF, SVG, AVIF). "
            "Returns a real local file and the image itself. Use this when the user requests an image artifact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "image_path": {"type": "string", "description": "Optional absolute path of an input image to edit."},
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of absolute paths of reference images.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "generate_video",
        "description": (
            "Generate a real video file using Gemini Enterprise's video generation tool, "
            "optionally conditioned on local reference image/file paths. Saves and returns the verified local video path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of absolute local image/file paths used as reference.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "analyze_media",
        "description": (
            "Upload one or more local multimodal files (PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, "
            "MP3, WAV, M4A, OGG, FLAC, MP4, MOV, WebM, PNG, HEIC, TIFF, etc.) to Gemini Enterprise "
            "and return its multimodal analysis or transcription."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Question or instruction about the attached file(s)."},
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of local files to upload and analyze.",
                },
            },
            "required": ["prompt", "file_paths"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
]


def _post_json(path: str, payload: dict, timeout: int = 950) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
            msg = body.get("error", {}).get("message") or body.get("detail") or str(exc)
        except Exception:
            msg = str(exc)
        raise ValueError(msg) from exc


def _upload_local_file(raw_path: str, expected_category: str | None = None) -> dict:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"文件路径必须是存在的绝对路径：{raw_path}")
    mime, category, norm_raw, safe_name = resolve_mime_and_name(
        path.read_bytes(),
        filename=path.name,
        expected_category=expected_category,
    )
    uploaded = _post_json(
        "/v1/files",
        {
            "filename": safe_name,
            "mime_type": mime,
            "file_data": base64.b64encode(norm_raw).decode("ascii"),
            "purpose": "user_data",
        },
        timeout=120,
    )
    return {**uploaded, "mime_type": mime, "category": category}


def call_generate_image(arguments: dict) -> dict:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    paths: list[str] = []
    if arguments.get("image_path"):
        paths.append(arguments["image_path"])
    if isinstance(arguments.get("image_paths"), list):
        paths.extend(arguments["image_paths"])

    payload: dict = {"prompt": prompt, "response_format": "b64_json"}
    if paths:
        payload["images"] = [_upload_local_file(p, expected_category="image")["id"] for p in paths]

    result = _post_json("/v1/images/generations", payload)
    image = result["data"][0]
    mime = image["mime_type"]
    _, raw = decode_base64_payload("data:" + mime + ";base64," + image["b64_json"], error_code="invalid_image")
    ext = SUPPORTED_MIME_SPECS.get(mime, ("image", "png"))[1]
    out_path = Path(__file__).resolve().parent / "generated" / f"codex-{uuid.uuid4().hex}.{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {
        "content": [
            {"type": "text", "text": f"Image saved: {out_path}"},
            {"type": "image", "mimeType": mime, "data": image["b64_json"]},
        ],
        "isError": False,
    }


def call_generate_video(arguments: dict) -> dict:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    refs = arguments.get("reference_paths") or []
    payload: dict = {"prompt": prompt, "response_format": "url"}
    if refs:
        payload["files"] = [_upload_local_file(p)["id"] for p in refs]
    result = _post_json("/v1/videos/generations", payload, timeout=1200)
    video = result["data"][0]
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "video_path": video.get("path"),
                        "url": video.get("url"),
                        "file_id": video.get("file_id"),
                        "mime_type": video.get("mime_type"),
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        "isError": False,
    }


def call_analyze_media(arguments: dict) -> dict:
    prompt = arguments.get("prompt")
    file_paths = arguments.get("file_paths")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    if not isinstance(file_paths, list) or not file_paths:
        raise ValueError("file_paths 必须是非空文件路径数组。")

    parts: list[dict] = [{"type": "text", "text": prompt}]
    for raw_p in file_paths:
        up = _upload_local_file(raw_p)
        cat = up["category"]
        if cat == "image":
            parts.append({"type": "image_url", "image_url": {"url": up["id"]}})
        elif cat == "audio":
            parts.append({"type": "input_audio", "file_id": up["id"], "filename": up["filename"]})
        elif cat == "video":
            parts.append({"type": "video_url", "file_id": up["id"], "filename": up["filename"]})
        else:
            parts.append({"type": "file", "file": {"file_id": up["id"], "filename": up["filename"], "mime_type": up["mime_type"]}})

    result = _post_json(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": parts}], "stream": False},
        timeout=950,
    )
    msg = result["choices"][0]["message"]
    text = msg.get("content") or ""
    if msg.get("attachments"):
        text += "\n\nGenerated attachments: " + json.dumps(msg["attachments"], ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(request: dict) -> dict:
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "gemini-bridge-multimodal", "version": "1.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        try:
            if name == "generate_image":
                return call_generate_image(args)
            if name == "generate_video":
                return call_generate_video(args)
            if name == "analyze_media":
                return call_analyze_media(args)
        except Exception as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        raise ValueError(f"Unknown tool: {name}")
    raise ValueError("Method not found: " + str(method))


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            try:
                result = {"jsonrpc": "2.0", "id": request["id"], "result": handle(request)}
            except ValueError as exc:
                result = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": str(exc)}}
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

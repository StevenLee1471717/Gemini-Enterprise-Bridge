"""Actual browser image generation and verified local image artifacts."""
from __future__ import annotations

import base64
from pathlib import Path
import time
import uuid

from bridge_hub import BridgeExecutionError, BridgeTimeoutError, BridgeUnavailableError
from media_inputs import FileStore, SUPPORTED_MIME_SPECS, decode_base64_payload, resolve_mime_and_name
from schemas import resolve_target_web_model


async def generate_images(payload, hub, context_builder, cfg, output_dir: Path, attachments=None, file_store: FileStore | None = None):
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    if int(payload.get("n", 1) or 1) != 1:
        raise ValueError("网页生图每次只支持一条提示词，n 请设为 1。")
    if payload.get("size", "auto") not in {"auto", None, ""}:
        raise ValueError("网页生图尺寸由网页决定，size 请使用 auto 或省略。")
    if payload.get("response_format", "b64_json") not in {"b64_json", "url"}:
        raise ValueError("response_format 只支持 b64_json 或 url。")
    if not hub.has_ready_client():
        raise BridgeUnavailableError("没有连接可用的 Gemini Enterprise 页面。")

    images = []
    files = []
    async with hub.acquire_slot(explicit_session_id="image_" + uuid.uuid4().hex, queue_timeout=cfg.queue_timeout_seconds) as slot:
        context_builder.clear_session(slot.slot_key)
        try:
            await hub.dispatch_complete_on_slot(
                slot=slot,
                prompt=prompt,
                target_model=resolve_target_web_model(payload.get("model") or cfg.default_model),
                reset_chat=True,
                suppress_citations=True,
                request_timeout=cfg.request_timeout_seconds,
                attachments=attachments or [],
                image_generation=True,
                output_images=images,
                output_files=files,
            )
        finally:
            context_builder.clear_session(slot.slot_key)

    candidates = images or files
    if not candidates:
        raise BridgeExecutionError("网页没有返回真实图片文件。")

    output_dir.mkdir(parents=True, exist_ok=True)
    cb_host = "127.0.0.1" if cfg.host in {"0.0.0.0", "::"} else cfg.host
    data = []
    for image in candidates:
        declared_mime, raw = decode_base64_payload(image.get("data_url") or "", error_code="invalid_image")
        mime, _, norm_raw, _ = resolve_mime_and_name(
            raw,
            declared_mime=image.get("mime_type") or declared_mime,
            expected_category="image",
        )
        extension = SUPPORTED_MIME_SPECS.get(mime, ("image", "png"))[1]
        name = uuid.uuid4().hex + "." + extension
        img_path = output_dir / name
        img_path.write_bytes(norm_raw)
        file_meta = None
        if file_store is not None:
            file_meta = file_store.save(norm_raw, filename=name, declared_mime=mime, purpose="assistants_output")
        if payload.get("response_format") == "url":
            entry = {"url": f"http://{cb_host}:{cfg.port}/v1/images/files/{name}", "mime_type": mime}
        else:
            entry = {"b64_json": base64.b64encode(norm_raw).decode("ascii"), "mime_type": mime}
        if file_meta is not None:
            entry["file_id"] = file_meta["id"]
        data.append(entry)
    return {"created": int(time.time()), "data": data}

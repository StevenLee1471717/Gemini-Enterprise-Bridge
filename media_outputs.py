"""Persist and format browser-generated multimodal outputs (images, videos, audio, documents)."""
from __future__ import annotations

import base64
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from bridge_hub import BridgeExecutionError, BridgeUnavailableError
from media_inputs import (
    FileStore,
     SUPPORTED_MIME_SPECS,
    decode_base64_payload,
    resolve_mime_and_name,
)
from schemas import resolve_target_web_model


def persist_browser_outputs(
    output_images: Optional[List[Dict[str, Any]]],
    output_files: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    file_store: FileStore,
    base_url: str,
) -> List[Dict[str, Any]]:
    """Validate, write to disk, register in `file_store`, and return structured artifact records."""
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_artifacts: List[Dict[str, Any]] = []
    seen_hashes: set[str] = set()

    combined: List[Dict[str, Any]] = []
    for img in output_images or []:
        if isinstance(img, dict):
            combined.append({**img, "_expected": "image"})
    for fitem in output_files or []:
        if isinstance(fitem, dict):
            combined.append(fitem)

    for item in combined:
        data_url = item.get("data_url") or item.get("data") or ""
        if not isinstance(data_url, str) or not data_url.strip():
            continue
        declared_mime, raw = decode_base64_payload(data_url)
        mime, category, norm_raw, safe_name = resolve_mime_and_name(
            raw,
            declared_mime=item.get("mime_type") or declared_mime,
            filename=item.get("name") or item.get("filename"),
            expected_category=item.get("_expected"),
            normalize_for_web=False,
        )
        meta = file_store.save(
            norm_raw,
            filename=safe_name,
            declared_mime=mime,
            purpose="assistants_output",
        )
        if meta["id"] in seen_hashes:
            continue
        seen_hashes.add(meta["id"])

        root_url = base_url.rstrip("/")
        file_url = f"{root_url}/v1/files/{meta['id']}/content"
        record: Dict[str, Any] = {
            "file_id": meta["id"],
            "filename": meta["filename"],
            "mime_type": mime,
            "category": category,
            "bytes": meta["bytes"],
            "url": file_url,
            "path": meta["path"],
        }
        if category == "image":
            ext = SUPPORTED_MIME_SPECS.get(mime, ("image", "png"))[1]
            img_name = f"{uuid.uuid4().hex}.{ext}"
            img_path = output_dir / img_name
            img_path.write_bytes(norm_raw)
            record["image_url"] = f"{root_url}/v1/images/files/{img_name}"
            record["image_path"] = str(img_path)
            record["b64_json"] = base64.b64encode(norm_raw).decode("ascii")
        saved_artifacts.append(record)

    return saved_artifacts


def format_artifacts_markdown(artifacts: List[Dict[str, Any]], existing_text: str = "") -> str:
    """Render markdown links/embeds for generated artifacts not already linked in `existing_text`."""
    lines: List[str] = []
    for art in artifacts:
        url = art.get("image_url") or art["url"]
        if url in existing_text or art["file_id"] in existing_text:
            continue
        fname = art["filename"]
        cat = art["category"]
        if cat == "image":
            lines.append(f"![{fname}]({url})")
        elif cat == "video":
            lines.append(f"[Generated Video: {fname}]({url})")
        elif cat == "audio":
            lines.append(f"[Generated Audio: {fname}]({url})")
        else:
            lines.append(f"[Generated File: {fname}]({url})")
    return "\n\n".join(lines)


async def generate_videos(payload: Dict[str, Any], hub, context_builder, cfg, output_dir: Path, file_store: FileStore, attachments=None) -> Dict[str, Any]:
    """Execute browser video generation (`Create videos`) and return verified local video artifacts."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    if payload.get("response_format", "url") not in {"b64_json", "url"}:
        raise ValueError("response_format 只支持 b64_json 或 url。")
    if not hub.has_ready_client():
        raise BridgeUnavailableError("没有连接可用的 Gemini Enterprise 页面。")

    raw_files: List[Dict[str, Any]] = []
    raw_images: List[Dict[str, Any]] = []
    async with hub.acquire_slot(explicit_session_id="video_" + uuid.uuid4().hex, queue_timeout=cfg.queue_timeout_seconds) as slot:
        context_builder.clear_session(slot.slot_key)
        try:
            await hub.dispatch_complete_on_slot(
                slot=slot,
                prompt=prompt,
                target_model=resolve_target_web_model(payload.get("model") or cfg.default_model),
                reset_chat=True,
                suppress_citations=True,
                request_timeout=max(cfg.request_timeout_seconds, 900.0),
                attachments=attachments or [],
                video_generation=True,
                output_images=raw_images,
                output_files=raw_files,
            )
        finally:
            context_builder.clear_session(slot.slot_key)

    cb_host = "127.0.0.1" if cfg.host in {"0.0.0.0", "::"} else cfg.host
    artifacts = persist_browser_outputs(raw_images, raw_files, output_dir, file_store, f"http://{cb_host}:{cfg.port}")
    videos = [a for a in artifacts if a["category"] == "video"] or artifacts
    if not videos:
        raise BridgeExecutionError("网页没有返回可下载的真实视频文件。")

    data = []
    for vid in videos:
        _, raw_bytes = file_store.read_bytes(vid["file_id"])
        if payload.get("response_format") == "b64_json":
            data.append({
                "b64_json": base64.b64encode(raw_bytes).decode("ascii"),
                "mime_type": vid["mime_type"],
                "file_id": vid["file_id"],
                "filename": vid["filename"],
                "path": vid["path"],
            })
        else:
            data.append({
                "url": vid["url"],
                "mime_type": vid["mime_type"],
                "file_id": vid["file_id"],
                "filename": vid["filename"],
                "path": vid["path"],
            })
    return {"created": int(time.time()), "data": data}

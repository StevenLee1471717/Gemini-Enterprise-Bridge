"""Backward-compatible image input helpers backed by unified `media_inputs`."""
from __future__ import annotations

from typing import Any, Sequence

from media_inputs import (
    InvalidAttachmentError,
    MAX_IMAGE_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES as MAX_TOTAL_IMAGE_BYTES,
    collect_attachments,
    decode_image,
    image_marker,
)
from schemas import ChatMessage


def collect_images(messages: Sequence[ChatMessage], **kwargs: Any) -> list[dict[str, Any]]:
    return collect_attachments(messages, **kwargs)


__all__ = [
    "InvalidAttachmentError",
    "MAX_IMAGE_BYTES",
    "MAX_TOTAL_IMAGE_BYTES",
    "collect_attachments",
    "collect_images",
    "decode_image",
    "image_marker",
]

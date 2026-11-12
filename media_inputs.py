"""Unified multimodal attachment validation, normalization, and local file store."""
from __future__ import annotations

import base64
import binascii
from email.parser import BytesParser
from email.policy import HTTP as EMAIL_HTTP_POLICY
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid

from schemas import ChatMessage

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS_PER_REQUEST = 10
INLINE_DATA_URL_MAX_BYTES = 1 * 1024 * 1024


class InvalidAttachmentError(ValueError):
    """Raised when an input image, document, audio, or video attachment is invalid."""

    def __init__(self, message: str, error_code: str = "invalid_attachment") -> None:
        super().__init__(message)
        self.error_code = error_code


# Canonical MIME -> (category, canonical_ext)
SUPPORTED_MIME_SPECS: Dict[str, Tuple[str, str]] = {
    # Images (native in browser)
    "image/png": ("image", "png"),
    "image/jpeg": ("image", "jpg"),
    "image/webp": ("image", "webp"),
    "image/gif": ("image", "gif"),
    "image/bmp": ("image", "bmp"),
    "image/svg+xml": ("image", "svg"),
    "image/avif": ("image", "avif"),
    "image/x-icon": ("image", "ico"),
    "image/vnd.microsoft.icon": ("image", "ico"),
    # Images (normalized to PNG via macOS sips when available)
    "image/heic": ("image", "heic"),
    "image/heif": ("image", "heif"),
    "image/tiff": ("image", "tiff"),
    "image/jp2": ("image", "jp2"),
    # Documents & Office
    "application/pdf": ("document", "pdf"),
    "application/msword": ("document", "doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("document", "docx"),
    "application/vnd.oasis.opendocument.text": ("document", "odt"),
    "application/rtf": ("document", "rtf"),
    "text/rtf": ("document", "rtf"),
    "application/vnd.ms-excel": ("document", "xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("document", "xlsx"),
    "application/vnd.oasis.opendocument.spreadsheet": ("document", "ods"),
    "application/vnd.ms-powerpoint": ("document", "ppt"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("document", "pptx"),
    "application/vnd.oasis.opendocument.presentation": ("document", "odp"),
    "application/epub+zip": ("document", "epub"),
    "application/zip": ("document", "zip"),
    "application/x-zip-compressed": ("document", "zip"),
    # Structured Data, Markup & Plain Text / Code
    "text/plain": ("document", "txt"),
    "text/markdown": ("document", "md"),
    "text/x-markdown": ("document", "md"),
    "text/csv": ("document", "csv"),
    "application/csv": ("document", "csv"),
    "text/tab-separated-values": ("document", "tsv"),
    "text/html": ("document", "html"),
    "application/xhtml+xml": ("document", "html"),
    "application/json": ("document", "json"),
    "application/ld+json": ("document", "json"),
    "application/x-ndjson": ("document", "jsonl"),
    "application/x-ipynb+json": ("document", "ipynb"),
    "application/xml": ("document", "xml"),
    "text/xml": ("document", "xml"),
    "text/yaml": ("document", "yaml"),
    "application/x-yaml": ("document", "yaml"),
    "application/yaml": ("document", "yaml"),
    "application/toml": ("document", "toml"),
    "text/css": ("document", "css"),
    "text/javascript": ("document", "js"),
    "application/javascript": ("document", "js"),
    "text/x-python": ("document", "py"),
    "application/x-python-code": ("document", "py"),
    "text/x-tex": ("document", "tex"),
    "application/x-tex": ("document", "tex"),
    "text/x-diff": ("document", "diff"),
    # Audio
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp3": ("audio", "mp3"),
    "audio/wav": ("audio", "wav"),
    "audio/x-wav": ("audio", "wav"),
    "audio/wave": ("audio", "wav"),
    "audio/mp4": ("audio", "m4a"),
    "audio/x-m4a": ("audio", "m4a"),
    "audio/m4a": ("audio", "m4a"),
    "audio/aac": ("audio", "aac"),
    "audio/ogg": ("audio", "ogg"),
    "audio/opus": ("audio", "opus"),
    "audio/flac": ("audio", "flac"),
    "audio/x-flac": ("audio", "flac"),
    "audio/webm": ("audio", "webm"),
    "audio/amr": ("audio", "amr"),
    "audio/aiff": ("audio", "aiff"),
    "audio/x-aiff": ("audio", "aiff"),
    # Video
    "video/mp4": ("video", "mp4"),
    "video/quicktime": ("video", "mov"),
    "video/webm": ("video", "webm"),
    "video/mpeg": ("video", "mpeg"),
    "video/x-msvideo": ("video", "avi"),
    "video/avi": ("video", "avi"),
    "video/x-matroska": ("video", "mkv"),
    "video/3gpp": ("video", "3gp"),
    "video/x-flv": ("video", "flv"),
}

EXT_TO_MIME: Dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "avif": "image/avif",
    "ico": "image/x-icon",
    "heic": "image/heic",
    "heif": "image/heif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "jp2": "image/jp2",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "epub": "application/epub+zip",
    "zip": "application/zip",
    "txt": "text/plain",
    "log": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "html": "text/html",
    "htm": "text/html",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "ipynb": "application/x-ipynb+json",
    "xml": "application/xml",
    "yaml": "text/yaml",
    "yml": "text/yaml",
    "toml": "application/toml",
    "diff": "text/plain",
    "patch": "text/plain",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "amr": "audio/amr",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mpeg": "video/mpeg",
    "mpg": "video/mpeg",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
    "3gp": "video/3gpp",
    "flv": "video/x-flv",
}

# Common code/text extensions normalized to text/plain with original filename kept
TEXT_CODE_EXTENSIONS = {
    "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "java", "c", "cc", "cpp", "cxx",
    "h", "hpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "kts", "sql", "sh",
    "bash", "zsh", "css", "scss", "less", "tex", "bib", "r", "lua", "dart", "scala",
    "pl", "vue", "svelte", "ini", "cfg", "conf", "env", "srt", "vtt",
}

MIME_ALIASES: Dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/x-ms-bmp": "image/bmp",
    "audio/x-mp3": "audio/mpeg",
    "audio/mp4a-latm": "audio/mp4",
    "video/x-m4v": "video/mp4",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_LOCAL_FILE_URL_RE = re.compile(
    r"^(?:https?://(?:127\.0\.0\.1|localhost)(?::\d+)?)?/v1/files/(file_[a-f0-9]{24,40})(?:/content)?$",
    re.IGNORECASE,
)
_LOCAL_IMAGE_URL_RE = re.compile(
    r"^(?:https?://(?:127\.0\.0\.1|localhost)(?::\d+)?)?/v1/images/files/([a-f0-9]{32}\.[a-z0-9]+)$",
    re.IGNORECASE,
)


def sanitize_filename(name: Optional[str], fallback_stem: str, ext: str) -> str:
    raw = Path(str(name or "").strip()).name
    if not raw:
        return f"{fallback_stem}.{ext}"
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._ ")
    if not cleaned:
        return f"{fallback_stem}.{ext}"
    if "." not in cleaned:
        return f"{cleaned}.{ext}"
    return cleaned[:120]


def image_marker(token: str) -> str:
    return "[Image attachment " + hashlib.sha256(token.encode("utf-8")).hexdigest()[:20] + "]"


def attachment_marker(category: str, token: str, filename: str = "", mime_type: str = "") -> str:
    if category == "image":
        return image_marker(token)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]
    label = {"audio": "Audio", "video": "Video"}.get(category, "File")
    meta = ": ".join(filter(None, [filename.strip(), mime_type.strip()]))
    if filename and mime_type:
        return f"[{label} attachment {digest}: {filename} ({mime_type})]"
    if meta:
        return f"[{label} attachment {digest}: {meta}]"
    return f"[{label} attachment {digest}]"


def _has_ftyp_brand(raw: bytes, brands: set[bytes]) -> bool:
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return False
    major = raw[8:12]
    if major in brands:
        return True
    box_len = int.from_bytes(raw[0:4], "big", signed=False)
    end = min(len(raw), max(16, min(box_len, 64)))
    for pos in range(16, end - 3, 4):
        if raw[pos : pos + 4] in brands:
            return True
    return False


def _looks_like_svg(raw: bytes) -> bool:
    head = raw[:2048].lstrip()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    lowered = head.lower()
    return b"<svg" in lowered and (lowered.startswith(b"<svg") or lowered.startswith(b"<?xml") or lowered.startswith(b"<!doctype svg"))


def _looks_like_text(raw: bytes) -> bool:
    if not raw:
        return False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in raw[:8192]:
        return False
    return True


def _sniff_mime_from_bytes(raw: bytes) -> Optional[str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if raw.startswith(b"BM") and len(raw) >= 14:
        return "image/bmp"
    if raw[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if raw[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n"):
        return "image/jp2"
    if _has_ftyp_brand(raw, {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}):
        return "image/heic"
    if _has_ftyp_brand(raw, {b"avif", b"avis"}):
        return "image/avif"
    if _looks_like_svg(raw):
        return "image/svg+xml"
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    if raw.startswith((b"{\rtf", b"{\\rtf")):
        return "application/rtf"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if len(raw) >= 12 and raw[:4] == b"FORM" and raw[8:12] in {b"AIFF", b"AIFC"}:
        return "audio/aiff"
    if raw.startswith(b"fLaC"):
        return "audio/flac"
    if raw.startswith(b"OggS"):
        return "audio/ogg"
    if raw.startswith(b"#!AMR"):
        return "audio/amr"
    if raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if _has_ftyp_brand(raw, {b"M4A ", b"M4B ", b"mp4a"}):
        return "audio/mp4"
    if _has_ftyp_brand(raw, {b"qt  "}):
        return "video/quicktime"
    if _has_ftyp_brand(raw, {b"3gp4", b"3gp5", b"3gp6", b"3g2a"}):
        return "video/3gpp"
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        return "video/mp4"
    if raw.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"AVI ":
        return "video/x-msvideo"
    if raw.startswith(b"FLV\x01"):
        return "video/x-flv"
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/msword"
    return None


def _validate_signature(mime: str, raw: bytes, category: str) -> bool:
    if not raw:
        return False
    if mime == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if mime == "image/webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if mime == "image/gif":
        return raw[:6] in {b"GIF87a", b"GIF89a"}
    if mime == "image/bmp":
        return raw.startswith(b"BM") and len(raw) >= 14
    if mime == "image/tiff":
        return raw[:4] in {b"II*\x00", b"MM\x00*"}
    if mime in {"image/x-icon", "image/vnd.microsoft.icon"}:
        return raw[:4] in {b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"} or raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/svg+xml":
        return _looks_like_svg(raw)
    if mime in {"image/heic", "image/heif"}:
        return _has_ftyp_brand(raw, {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"})
    if mime == "image/avif":
        return _has_ftyp_brand(raw, {b"avif", b"avis", b"mif1"})
    if mime == "image/jp2":
        return raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or raw[:4] == b"\xff\x4f\xff\x51"
    if mime == "application/pdf":
        return raw.startswith(b"%PDF-") or b"%PDF-" in raw[:1024]
    if mime in {
        "application/zip",
        "application/x-zip-compressed",
        "application/epub+zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
    }:
        return raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if mime in {"application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"}:
        return raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or raw.startswith(b"{\\rtf")
    if mime in {"application/rtf", "text/rtf"}:
        return raw.lstrip().startswith(b"{\\rtf") or _looks_like_text(raw)
    if category == "document" and (mime.startswith("text/") or mime in {
        "application/json", "application/ld+json", "application/x-ndjson",
        "application/x-ipynb+json", "application/xml", "application/x-yaml",
        "application/yaml", "application/toml", "application/csv", "application/xhtml+xml",
        "application/javascript", "application/x-python-code", "application/x-tex",
    }):
        return _looks_like_text(raw)
    if mime in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    if mime in {"audio/flac", "audio/x-flac"}:
        return raw.startswith(b"fLaC")
    if mime in {"audio/ogg", "audio/opus"}:
        return raw.startswith(b"OggS")
    if mime in {"audio/aiff", "audio/x-aiff"}:
        return len(raw) >= 12 and raw[:4] == b"FORM" and raw[8:12] in {b"AIFF", b"AIFC"}
    if mime in {"audio/mpeg", "audio/mp3"}:
        return raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0) or _has_ftyp_brand(raw, {b"M4A ", b"mp4a"})
    if mime in {"audio/mp4", "audio/x-m4a", "audio/m4a"}:
        return (len(raw) >= 8 and raw[4:8] in {b"ftyp", b"moov", b"mdat"})
    if mime == "audio/aac":
        return (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xF0) == 0xF0) or (len(raw) >= 8 and raw[4:8] == b"ftyp") or raw.startswith(b"ADIF")
    if mime in {"audio/webm", "video/webm", "video/x-matroska"}:
        return raw.startswith(b"\x1a\x45\xdf\xa3")
    if mime == "audio/amr":
        return raw.startswith(b"#!AMR")
    if mime in {"video/mp4", "video/quicktime", "video/3gpp"}:
        return len(raw) >= 8 and raw[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}
    if mime in {"video/x-msvideo", "video/avi"}:
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"AVI "
    if mime == "video/x-flv":
        return raw.startswith(b"FLV")
    if mime == "video/mpeg":
        return len(raw) >= 4 and raw[:3] == b"\x00\x00\x01" or (raw[0] == 0x47)
    return len(raw) > 0


def _find_binary(name: str, extra_paths: Sequence[str] = ()) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra_paths:
        if Path(candidate).is_file():
            return candidate
    return None


def _convert_image_to_png_if_needed(mime: str, raw: bytes, filename: str) -> Tuple[str, bytes, str]:
    """Normalize non-web-composer image formats (HEIC/HEIF/TIFF/BMP/ICO/AVIF/JP2) to PNG."""
    if mime not in {
        "image/heic", "image/heif", "image/tiff", "image/jp2",
        "image/bmp", "image/x-icon", "image/vnd.microsoft.icon", "image/avif",
    }:
        return mime, raw, filename
    ext = SUPPORTED_MIME_SPECS[mime][1]
    stem = Path(filename).stem or "image"
    sips = _find_binary("sips", ("/usr/bin/sips",))
    ffmpeg = _find_binary("ffmpeg", ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"))
    with tempfile.TemporaryDirectory() as tmpdir:
        src_p = Path(tmpdir) / f"input.{ext}"
        dst_p = Path(tmpdir) / "output.png"
        src_p.write_bytes(raw)
        if sips:
            proc = subprocess.run(
                [sips, "-s", "format", "png", str(src_p), "--out", str(dst_p)],
                capture_output=True,
                timeout=25,
            )
            if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
                return "image/png", dst_p.read_bytes(), f"{stem}.png"
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(src_p), "-frames:v", "1", str(dst_p)],
                capture_output=True,
                timeout=25,
            )
            if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
                return "image/png", dst_p.read_bytes(), f"{stem}.png"
    return mime, raw, filename


def _normalize_audio_for_web(mime: str, raw: bytes, filename: str) -> Tuple[str, bytes, str]:
    """Convert WAV/AIFF/FLAC/OGG/OPUS/AAC/WEBM/AMR to M4A (AAC) accepted by Gemini Enterprise web."""
    if mime in {"audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/m4a"}:
        norm_mime = "audio/mpeg" if "mpeg" in mime or "mp3" in mime else "audio/mp4"
        return norm_mime, raw, filename
    ext = SUPPORTED_MIME_SPECS.get(mime, ("audio", "wav"))[1]
    stem = Path(filename).stem or "audio"
    afconvert = _find_binary("afconvert", ("/usr/bin/afconvert",))
    ffmpeg = _find_binary("ffmpeg", ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"))
    with tempfile.TemporaryDirectory() as tmpdir:
        src_p = Path(tmpdir) / f"input.{ext}"
        dst_p = Path(tmpdir) / "output.m4a"
        src_p.write_bytes(raw)
        if afconvert and mime in {"audio/wav", "audio/x-wav", "audio/wave", "audio/aiff", "audio/x-aiff", "audio/flac", "audio/x-flac", "audio/aac"}:
            proc = subprocess.run(
                [afconvert, "-f", "m4af", "-d", "aac", str(src_p), str(dst_p)],
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
                return "audio/mp4", dst_p.read_bytes(), f"{stem}.m4a"
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(src_p), "-vn", "-c:a", "aac", "-b:a", "128k", str(dst_p)],
                capture_output=True,
                timeout=45,
            )
            if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
                return "audio/mp4", dst_p.read_bytes(), f"{stem}.m4a"
    return mime, raw, filename


def _normalize_video_for_web(mime: str, raw: bytes, filename: str) -> Tuple[str, bytes, str]:
    """Convert MOV/WebM/MKV/AVI/MPEG/3GP/FLV to H.264+AAC MP4 so Gemini Enterprise decodes frames & audio."""
    if mime == "video/mp4":
        return mime, raw, filename
    ffmpeg = _find_binary("ffmpeg", ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"))
    if not ffmpeg:
        return mime, raw, filename
    ext = SUPPORTED_MIME_SPECS.get(mime, ("video", "mov"))[1]
    stem = Path(filename).stem or "video"
    with tempfile.TemporaryDirectory() as tmpdir:
        src_p = Path(tmpdir) / f"input.{ext}"
        dst_p = Path(tmpdir) / "output.mp4"
        src_p.write_bytes(raw)
        proc = subprocess.run(
            [
                ffmpeg, "-y", "-i", str(src_p),
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "+faststart", str(dst_p),
            ],
            capture_output=True,
            timeout=90,
        )
        if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
            return "video/mp4", dst_p.read_bytes(), f"{stem}.mp4"
    return mime, raw, filename


def _normalize_document_for_web(mime: str, raw: bytes, filename: str) -> Tuple[str, bytes, str]:
    """Convert RTF to DOCX via macOS textutil because Gemini Enterprise web composer ignores raw .rtf."""
    if mime not in {"application/rtf", "text/rtf"}:
        return mime, raw, filename
    textutil = _find_binary("textutil", ("/usr/bin/textutil",))
    if not textutil:
        return mime, raw, filename
    stem = Path(filename).stem or "document"
    with tempfile.TemporaryDirectory() as tmpdir:
        src_p = Path(tmpdir) / "input.rtf"
        dst_p = Path(tmpdir) / "output.docx"
        src_p.write_bytes(raw)
        proc = subprocess.run(
            [textutil, "-convert", "docx", str(src_p), "-output", str(dst_p)],
            capture_output=True,
            timeout=20,
        )
        if proc.returncode == 0 and dst_p.is_file() and dst_p.stat().st_size > 0:
            return (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                dst_p.read_bytes(),
                f"{stem}.docx",
            )
    return mime, raw, filename


def resolve_mime_and_name(
    raw: bytes,
    declared_mime: Optional[str] = None,
    filename: Optional[str] = None,
    expected_category: Optional[str] = None,
    normalize_for_web: bool = True,
) -> Tuple[str, str, bytes, str]:
    """Validate, normalize, and return (mime_type, category, normalized_bytes, safe_filename)."""
    err_code = {
        "image": "invalid_image",
        "audio": "invalid_audio_input",
    }.get(expected_category or "", "invalid_attachment")

    if not raw:
        raise InvalidAttachmentError("附件内容为空。", error_code=err_code)

    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    mime = MIME_ALIASES.get(mime, mime)
    ext_from_name = Path(filename or "").suffix.lstrip(".").lower()

    if (not mime or mime == "application/octet-stream") and ext_from_name:
        if ext_from_name in EXT_TO_MIME:
            mime = EXT_TO_MIME[ext_from_name]
        elif ext_from_name in TEXT_CODE_EXTENSIONS:
            mime = "text/plain"
        else:
            guessed, _ = mimetypes.guess_type(f"file.{ext_from_name}")
            if guessed:
                mime = MIME_ALIASES.get(guessed.lower(), guessed.lower())

    if not mime or mime == "application/octet-stream":
        sniffed = _sniff_mime_from_bytes(raw)
        if sniffed:
            mime = sniffed
        elif _looks_like_text(raw):
            mime = "text/plain"

    # Allow code/text subtypes like text/x-csrc or application/x-sh
    if mime not in SUPPORTED_MIME_SPECS:
        if ext_from_name in TEXT_CODE_EXTENSIONS and _looks_like_text(raw):
            mime = "text/plain"
        elif mime.startswith("text/") and _looks_like_text(raw):
            mime = "text/plain"
        else:
            raise InvalidAttachmentError(
                f"暂不支持该媒体或文件格式：{declared_mime or ext_from_name or 'unknown'}。",
                error_code=err_code,
            )

    # Refine generic ZIP/OLE2 or WebM audio vs video when filename gives a specific Office/media extension
    if mime == "application/zip" and ext_from_name in {"docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"}:
        mime = EXT_TO_MIME[ext_from_name]
    elif mime == "application/msword" and ext_from_name in {"xls", "ppt"}:
        mime = EXT_TO_MIME[ext_from_name]
    elif mime == "video/webm" and (expected_category == "audio" or ext_from_name in {"weba"}):
        mime = "audio/webm"

    category, canonical_ext = SUPPORTED_MIME_SPECS[mime]
    if expected_category and category != expected_category:
        # Allow audio/webm or video/mp4 containers used as audio input (e.g. m4a/mp4 voice recordings)
        if expected_category == "audio" and mime in {"video/webm", "video/mp4", "video/3gpp"}:
            category = "audio"
            mime = "audio/webm" if mime == "video/webm" else "audio/mp4"
            canonical_ext = SUPPORTED_MIME_SPECS[mime][1]
        else:
            raise InvalidAttachmentError(
                f"附件类型不匹配：期望 {expected_category}，实际为 {mime}。",
                error_code=err_code,
            )

    max_bytes = MAX_IMAGE_BYTES if category == "image" else MAX_ATTACHMENT_BYTES
    if len(raw) > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise InvalidAttachmentError(f"单个附件不得超过 {limit_mb} MB。", error_code=err_code)

    if not _validate_signature(mime, raw, category):
        raise InvalidAttachmentError("附件为空、损坏或实际文件头与声明格式不符。", error_code=err_code)

    preserve_ext = ext_from_name if (ext_from_name in EXT_TO_MIME or ext_from_name in TEXT_CODE_EXTENSIONS) else canonical_ext
    safe_name = sanitize_filename(filename, f"bridge-{category}", preserve_ext)

    if normalize_for_web:
        if category == "image":
            mime, raw, safe_name = _convert_image_to_png_if_needed(mime, raw, safe_name)
        elif category == "audio":
            mime, raw, safe_name = _normalize_audio_for_web(mime, raw, safe_name)
        elif category == "video":
            mime, raw, safe_name = _normalize_video_for_web(mime, raw, safe_name)
        elif category == "document":
            mime, raw, safe_name = _normalize_document_for_web(mime, raw, safe_name)

    # Normalize exotic text/code MIME types to text/plain for Gemini Enterprise web composer compatibility
    browser_mime = mime
    if category == "document" and mime in {
        "application/x-ndjson", "application/x-ipynb+json", "application/toml",
        "application/x-yaml", "application/yaml", "text/yaml", "text/x-markdown",
        "text/x-python", "application/x-python-code", "text/x-tex", "application/x-tex", "text/x-diff",
    }:
        browser_mime = "text/plain" if not mime.endswith("json") else "application/json"

    return browser_mime, category, raw, safe_name


def decode_base64_payload(value: str, error_code: str = "invalid_attachment") -> Tuple[Optional[str], bytes]:
    """Decode either a `data:<mime>;base64,...` URL or a raw base64 string."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidAttachmentError("附件内容不能为空。", error_code=error_code)
    stripped = value.strip()
    if stripped.startswith("data:"):
        if "," not in stripped:
            raise InvalidAttachmentError("data URL 格式无效。", error_code=error_code)
        header, data_part = stripped.split(",", 1)
        meta = header[5:]
        parts = [p.strip() for p in meta.split(";") if p.strip()]
        is_b64 = "base64" in [p.lower() for p in parts]
        mime = parts[0].lower() if parts and "/" in parts[0] else "application/octet-stream"
        if not is_b64:
            from urllib.parse import unquote_to_bytes
            return mime, unquote_to_bytes(data_part)
        cleaned = re.sub(r"\s+", "", data_part)
        try:
            return mime, base64.b64decode(cleaned, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InvalidAttachmentError("附件 base64 内容无效。", error_code=error_code) from exc

    if re.match(r"^https?://", stripped, re.IGNORECASE):
        raise InvalidAttachmentError(
            "不支持直接拉取外部远程网址；请先通过 /v1/files 上传或传入 base64 / data URL。",
            error_code=error_code,
        )

    cleaned = re.sub(r"\s+", "", stripped)
    try:
        return None, base64.b64decode(cleaned, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidAttachmentError("附件 base64 内容或文件引用无效。", error_code=error_code) from exc


def decode_image(url: str) -> Tuple[str, bytes]:
    """Backward-compatible image decoder supporting common image formats."""
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
        raise InvalidAttachmentError(
            "图片必须使用 data:image/...;base64 内容或已上传的本地 file_id；不支持外部远程网址。",
            error_code="invalid_image",
        )
    declared_mime, raw = decode_base64_payload(url, error_code="invalid_image")
    mime, _, norm_raw, _ = resolve_mime_and_name(raw, declared_mime=declared_mime, expected_category="image")
    return mime, norm_raw


class FileStore:
    """Local disk-backed OpenAI-compatible `/v1/files` and attachment store."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.files_dir = root_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, file_id: str) -> Path:
        return self.files_dir / f"{file_id}.json"

    def _valid_id(self, file_id: str) -> bool:
        return bool(re.fullmatch(r"file_[a-f0-9]{32}", file_id or ""))

    def save(
        self,
        raw: bytes,
        filename: Optional[str] = None,
        declared_mime: Optional[str] = None,
        purpose: str = "user_data",
        expected_category: Optional[str] = None,
    ) -> Dict[str, Any]:
        mime, category, norm_raw, safe_name = resolve_mime_and_name(
            raw,
            declared_mime=declared_mime,
            filename=filename,
            expected_category=expected_category,
            normalize_for_web=(purpose != "assistants_output"),
        )
        digest = hashlib.sha256(norm_raw).hexdigest()[:32]
        file_id = f"file_{digest}"
        blob_dir = self.files_dir / file_id
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob_path = blob_dir / safe_name
        if not blob_path.is_file():
            blob_path.write_bytes(norm_raw)
        meta = {
            "id": file_id,
            "object": "file",
            "bytes": len(norm_raw),
            "created_at": int(time.time()),
            "filename": safe_name,
            "purpose": purpose or "user_data",
            "status": "processed",
            "mime_type": mime,
            "category": category,
            "path": str(blob_path),
        }
        self._meta_path(file_id).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def get_meta(self, file_id: str) -> Dict[str, Any]:
        if not self._valid_id(file_id):
            raise FileNotFoundError(f"文件不存在：{file_id}")
        mp = self._meta_path(file_id)
        if not mp.is_file():
            raise FileNotFoundError(f"文件不存在：{file_id}")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        if not Path(meta.get("path", "")).is_file():
            raise FileNotFoundError(f"文件数据已丢失：{file_id}")
        return meta

    def read_bytes(self, file_id: str) -> Tuple[Dict[str, Any], bytes]:
        meta = self.get_meta(file_id)
        return meta, Path(meta["path"]).read_bytes()

    def list_files(self, purpose: Optional[str] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for mp in sorted(self.files_dir.glob("file_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
                if not Path(meta.get("path", "")).is_file():
                    continue
                if purpose and meta.get("purpose") != purpose:
                    continue
                items.append({k: v for k, v in meta.items() if k != "path"})
            except Exception:
                continue
        return items

    def delete(self, file_id: str) -> bool:
        if not self._valid_id(file_id):
            return False
        mp = self._meta_path(file_id)
        blob_dir = self.files_dir / file_id
        existed = mp.is_file() or blob_dir.is_dir()
        mp.unlink(missing_ok=True)
        if blob_dir.is_dir():
            shutil.rmtree(blob_dir, ignore_errors=True)
        return existed


def parse_multipart_form(content_type: str, body: bytes) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Stdlib-only multipart/form-data parser (no python-multipart dependency required)."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise InvalidAttachmentError("请求必须是 multipart/form-data 或 JSON。")
    raw_msg = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    msg = BytesParser(policy=EMAIL_HTTP_POLICY).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise InvalidAttachmentError("无法解析 multipart/form-data 请求体。")
    fields: Dict[str, str] = {}
    files: List[Dict[str, Any]] = []
    for part in msg.iter_parts():
        disp = part.get_content_disposition()
        if disp != "form-data":
            continue
        name = part.get_param("name", header="content-disposition") or ""
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None or name in {"file", "image", "image[]", "mask"}:
            if filename is None and name not in {"file", "image", "image[]", "mask"}:
                fields[name] = payload.decode("utf-8", errors="replace")
            elif filename is None and len(payload) < 256 and part.get_content_type() == "text/plain":
                fields[name] = payload.decode("utf-8", errors="replace")
            else:
                files.append(
                    {
                        "field": name,
                        "filename": filename or f"{name}.bin",
                        "content_type": part.get_content_type(),
                        "data": payload,
                    }
                )
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


def extract_part_marker(part: Dict[str, Any]) -> Optional[str]:
    """Compute the deterministic prompt placeholder marker for a multimodal content part."""
    kind = str(part.get("type") or "").lower()
    if kind in {"image_url", "input_image", "image"}:
        if kind == "image" and isinstance(part.get("source"), dict):
            src = part["source"]
            token = src.get("file_id") or src.get("url") or src.get("data") or ""
            return image_marker(str(token))
        val = part.get("image_url")
        url = val.get("url", "") if isinstance(val, dict) else str(val or part.get("file_id") or "")
        return image_marker(url)

    if kind in {"file", "input_file", "document"}:
        inner = part.get("file") if isinstance(part.get("file"), dict) else part
        if kind == "document" and isinstance(part.get("source"), dict):
            src = part["source"]
            token = str(src.get("file_id") or src.get("data") or "")
            fname = str(part.get("title") or inner.get("filename") or "document")
            mime = str(src.get("media_type") or "")
            return attachment_marker("document", token, fname, mime)
        token = str(inner.get("file_id") or inner.get("file_url") or inner.get("file_data") or inner.get("data") or "")
        fname = str(inner.get("filename") or part.get("filename") or "")
        mime = str(inner.get("mime_type") or part.get("mime_type") or "")
        return attachment_marker("document", token, fname, mime)

    if kind in {"input_audio", "audio_url", "audio"}:
        inner = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else (
            part.get("audio_url") if isinstance(part.get("audio_url"), dict) else part
        )
        token = str(inner.get("file_id") or inner.get("url") or inner.get("audio_url") or inner.get("data") or "")
        fmt = str(inner.get("format") or part.get("format") or "").lower()
        fname = str(inner.get("filename") or (f"audio.{fmt}" if fmt else "audio"))
        mime = EXT_TO_MIME.get(fmt, "") if fmt else ""
        return attachment_marker("audio", token, fname, mime)

    if kind in {"video_url", "input_video", "video"}:
        inner = part.get("video_url") if isinstance(part.get("video_url"), dict) else part
        token = str(inner.get("file_id") or inner.get("url") or inner.get("video_url") or inner.get("file_data") or inner.get("data") or "")
        fname = str(inner.get("filename") or part.get("filename") or "video")
        mime = str(inner.get("mime_type") or "")
        return attachment_marker("video", token, fname, mime)

    return None


def _resolve_reference_or_bytes(
    *, 
    raw_value: Optional[str],
    file_id: Optional[str],
    filename: Optional[str],
    declared_mime: Optional[str],
    expected_category: Optional[str],
    file_store: Optional[FileStore],
    image_output_dir: Optional[Path],
) -> Tuple[str, str, bytes, str, str]:
    """Return (mime, category, bytes, safe_filename, marker_token)."""
    err_code = {
        "image": "invalid_image",
        "audio": "invalid_audio_input",
    }.get(expected_category or "", "invalid_attachment")

    candidate_id = (file_id or "").strip()
    val = (raw_value or "").strip()

    if not candidate_id and val:
        m_file = _LOCAL_FILE_URL_RE.match(val)
        if m_file:
            candidate_id = m_file.group(1)
        elif re.fullmatch(r"file_[a-f0-9]{24,40}", val):
            candidate_id = val
        else:
            m_img = _LOCAL_IMAGE_URL_RE.match(val)
            if m_img and image_output_dir is not None:
                img_path = image_output_dir / m_img.group(1)
                if not img_path.is_file():
                    raise InvalidAttachmentError(f"本地图片不存在：{m_img.group(1)}", error_code=err_code)
                raw = img_path.read_bytes()
                mime, cat, norm_raw, safe_name = resolve_mime_and_name(
                    raw, declared_mime=declared_mime, filename=img_path.name, expected_category=expected_category
                )
                return mime, cat, norm_raw, safe_name, val

    if candidate_id:
        if file_store is None:
            raise InvalidAttachmentError("当前实例未启用本地文件存储。", error_code=err_code)
        try:
            meta, raw = file_store.read_bytes(candidate_id)
        except FileNotFoundError as exc:
            raise InvalidAttachmentError(f"找不到已上传文件：{candidate_id}", error_code=err_code) from exc
        mime, cat, norm_raw, safe_name = resolve_mime_and_name(
            raw,
            declared_mime=declared_mime or meta.get("mime_type"),
            filename=filename or meta.get("filename"),
            expected_category=expected_category,
        )
        token = val or candidate_id
        return mime, cat, norm_raw, safe_name, token

    if not val:
        raise InvalidAttachmentError("缺少附件数据（data URL、base64 或 file_id）。", error_code=err_code)

    parsed_mime, raw = decode_base64_payload(val, error_code=err_code)
    mime, cat, norm_raw, safe_name = resolve_mime_and_name(
        raw,
        declared_mime=declared_mime or parsed_mime,
        filename=filename,
        expected_category=expected_category,
    )
    return mime, cat, norm_raw, safe_name, val


def collect_attachments(
    messages: Sequence[ChatMessage],
    file_store: Optional[FileStore] = None,
    callback_base_url: str = "",
    image_output_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Extract, validate, and prepare all multimodal attachments referenced in `messages`."""
    attachments: List[Dict[str, Any]] = []
    seen_markers: set[str] = set()
    total_bytes = 0

    for message in messages:
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "").lower()
            if kind not in {
                "image_url", "input_image", "image",
                "file", "input_file", "document",
                "input_audio", "audio_url", "audio",
                "video_url", "input_video", "video",
            }:
                continue

            expected_cat: Optional[str] = None
            raw_val: Optional[str] = None
            fid: Optional[str] = None
            fname: Optional[str] = None
            decl_mime: Optional[str] = None

            if kind in {"image_url", "input_image", "image"}:
                expected_cat = "image"
                if kind == "image" and isinstance(part.get("source"), dict):
                    src = part["source"]
                    fid = src.get("file_id")
                    raw_val = src.get("url") or src.get("data")
                    decl_mime = src.get("media_type")
                else:
                    val = part.get("image_url")
                    if isinstance(val, dict):
                        raw_val = val.get("url")
                        fid = val.get("file_id") or part.get("file_id")
                    else:
                        raw_val = val if isinstance(val, str) else None
                        fid = part.get("file_id")
                marker = extract_part_marker(part) or ""

            elif kind in {"file", "input_file", "document"}:
                expected_cat = None  # Let PDF, DOCX, CSV, or even an image/audio uploaded as `input_file` work!
                if kind == "document" and isinstance(part.get("source"), dict):
                    src = part["source"]
                    fid = src.get("file_id")
                    raw_val = src.get("data") or src.get("url")
                    decl_mime = src.get("media_type")
                    fname = part.get("title") or part.get("filename")
                else:
                    inner = part.get("file") if isinstance(part.get("file"), dict) else part
                    fid = inner.get("file_id") or part.get("file_id")
                    raw_val = inner.get("file_data") or inner.get("file_url") or inner.get("data") or inner.get("url")
                    fname = inner.get("filename") or part.get("filename")
                    decl_mime = inner.get("mime_type") or part.get("mime_type")
                marker = extract_part_marker(part) or ""

            elif kind in {"input_audio", "audio_url", "audio"}:
                expected_cat = "audio"
                inner = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else (
                    part.get("audio_url") if isinstance(part.get("audio_url"), dict) else part
                )
                fid = inner.get("file_id") or part.get("file_id")
                raw_val = inner.get("url") or inner.get("audio_url") or inner.get("data")
                fmt = str(inner.get("format") or part.get("format") or "").lower().lstrip(".")
                if fmt in EXT_TO_MIME:
                    decl_mime = EXT_TO_MIME[fmt]
                fname = inner.get("filename") or (f"audio-{len(attachments)+1}.{fmt}" if fmt else None)
                marker = extract_part_marker(part) or ""

            else:  # video_url | input_video | video
                expected_cat = "video"
                inner = part.get("video_url") if isinstance(part.get("video_url"), dict) else part
                fid = inner.get("file_id") or part.get("file_id")
                raw_val = inner.get("url") or inner.get("video_url") or inner.get("file_data") or inner.get("data")
                fname = inner.get("filename") or part.get("filename")
                decl_mime = inner.get("mime_type") or part.get("mime_type")
                marker = extract_part_marker(part) or ""

            mime, cat, norm_raw, safe_name, token = _resolve_reference_or_bytes(
                raw_value=raw_val,
                file_id=fid,
                filename=fname,
                declared_mime=decl_mime,
                expected_category=expected_cat,
                file_store=file_store,
                image_output_dir=image_output_dir,
            )
            if not marker:
                marker = attachment_marker(cat, token, safe_name, mime)
            if marker in seen_markers:
                continue
            seen_markers.add(marker)

            total_bytes += len(norm_raw)
            if len(attachments) >= MAX_ATTACHMENTS_PER_REQUEST or total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
                err_code = "invalid_image" if expected_cat == "image" else "invalid_attachment"
                raise InvalidAttachmentError(
                    f"一次请求最多上传 {MAX_ATTACHMENTS_PER_REQUEST} 个附件，合计不得超过 {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB。",
                    error_code=err_code,
                )

            ext = SUPPORTED_MIME_SPECS.get(mime, (cat, "bin"))[1]
            if not fname and not fid and safe_name.startswith("bridge-"):
                safe_name = f"bridge-{len(attachments) + 1}.{ext}"

            entry: Dict[str, Any] = {
                "id": marker,
                "name": safe_name,
                "mime_type": mime,
                "category": cat,
                "size_bytes": len(norm_raw),
            }
            if len(norm_raw) <= INLINE_DATA_URL_MAX_BYTES or file_store is None or not callback_base_url:
                entry["data_url"] = f"data:{mime};base64," + base64.b64encode(norm_raw).decode("ascii")
            else:
                saved = file_store.save(norm_raw, filename=safe_name, declared_mime=mime, purpose="bridge_staging")
                entry["file_id"] = saved["id"]
                entry["_path"] = saved["path"]
                entry["fetch_url"] = f"{callback_base_url.rstrip('/')}/api/bridge/attachments/{saved['id']}"
            attachments.append(entry)

    return attachments


def compact_messages_for_store(messages: Sequence[Dict[str, Any]], file_store: Optional[FileStore]) -> List[Dict[str, Any]]:
    """Replace large inline base64 payloads with local `file_id` references before caching in ResponseStore."""
    if file_store is None:
        return list(messages)
    compacted: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            compacted.append(msg)
            continue
        new_parts = []
        changed = False
        for part in content:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            serialized_len = len(json.dumps(part, ensure_ascii=False))
            if serialized_len < 128 * 1024:
                new_parts.append(part)
                continue
            try:
                extracted = collect_attachments([ChatMessage(role="user", content=[part])], file_store=file_store)
                if extracted:
                    att = extracted[0]
                    if att.get("file_id"):
                        fid = att["file_id"]
                    else:
                        _, raw = decode_base64_payload(att["data_url"])
                        fid = file_store.save(raw, filename=att["name"], declared_mime=att["mime_type"], purpose="response_cache")["id"]
                    cat = att["category"]
                    if cat == "image":
                        new_parts.append({"type": "image_url", "image_url": {"url": fid}})
                    elif cat == "audio":
                        new_parts.append({"type": "input_audio", "file_id": fid, "filename": att["name"]})
                    elif cat == "video":
                        new_parts.append({"type": "video_url", "file_id": fid, "filename": att["name"]})
                    else:
                        new_parts.append({"type": "file", "file": {"file_id": fid, "filename": att["name"], "mime_type": att["mime_type"]}})
                    changed = True
                    continue
            except Exception:
                pass
            new_parts.append(part)
        compacted.append({**msg, "content": new_parts} if changed else msg)
    return compacted

"""Multimodal content normalization, MIME/magic-byte detection, and document extraction."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import mimetypes
import os
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import httpx

# Curated extension -> MIME map (takes precedence over OS registry so Windows does not
# misclassify `.ts` as MPEG transport stream video or miss `.md`/`.jsonl`/Office types).
EXT_TO_MIME: Dict[str, str] = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    # Audio
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".aiff": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    # Video
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    # Documents & Archives
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
    # Text, Markup, Data & Code
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".tex": "text/x-tex",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".jsx": "text/jsx",
    ".ts": "text/typescript",
    ".mts": "text/typescript",
    ".cts": "text/typescript",
    ".tsx": "text/tsx",
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".ipynb": "application/x-ipynb+json",
    ".java": "text/x-java",
    ".kt": "text/x-kotlin",
    ".scala": "text/x-scala",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++src",
    ".cc": "text/x-c++src",
    ".cxx": "text/x-c++src",
    ".hpp": "text/x-c++hdr",
    ".cs": "text/x-csharp",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".swift": "text/x-swift",
    ".r": "text/x-r",
    ".sql": "application/sql",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".ps1": "text/plain",
    ".bat": "text/plain",
    ".cmd": "text/plain",
    ".vue": "text/plain",
    ".svelte": "text/plain",
}

MIME_TO_EXT: Dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/rtf": ".rtf",
    "application/epub+zip": ".epub",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/json": ".json",
    "application/x-ndjson": ".jsonl",
    "application/yaml": ".yaml",
    "text/yaml": ".yaml",
    "application/toml": ".toml",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "text/typescript": ".ts",
    "text/tsx": ".tsx",
    "text/jsx": ".jsx",
    "text/x-python": ".py",
    "application/sql": ".sql",
}

AUDIO_FORMAT_TO_MIME: Dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "webm": "audio/webm",
    "pcm16": "audio/wav",
    "pcm": "audio/wav",
}

TEXT_LIKE_MIMES = {
    "application/json",
    "application/ld+json",
    "application/x-ndjson",
    "application/x-ipynb+json",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-javascript",
    "application/typescript",
    "application/sql",
    "application/x-sh",
    "application/x-httpd-php",
    "image/svg+xml",
}

OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_DATA_URI_RE = re.compile(r"^data:([^,]*?),(.*)$", re.DOTALL | re.IGNORECASE)
_SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_CONTENT_DISPOSITION_FILENAME_RE = re.compile(
    r"filename\*=UTF-8''([^;\s]+)|filename=\"?([^\";]+)\"?",
    re.IGNORECASE,
)
_BLOCKED_LOCAL_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
_BLOCKED_LOCAL_SUFFIXES = (".pem", ".key", ".pfx", ".p12")


def _clean_mime(raw_mime: Optional[str]) -> str:
    if not raw_mime:
        return ""
    base = str(raw_mime).split(";", 1)[0].strip().lower()
    if base == "image/jpg":
        return "image/jpeg"
    if base == "audio/mp3":
        return "audio/mpeg"
    if base in {"audio/x-wav", "audio/wave"}:
        return "audio/wav"
    if base == "text/x-markdown":
        return "text/markdown"
    return base


def sniff_mime_from_bytes(data: bytes) -> str:
    """Infer MIME type from leading magic bytes (including OOXML ZIP inspection)."""
    if not data:
        return ""
    head = data[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"M4A ", b"M4B "}:
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in {b"heic", b"heix", b"mif1", b"msf1"}:
            return "image/heic"
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        return "video/mp4"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names or any(n.startswith("word/") for n in names):
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names):
                    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if "ppt/presentation.xml" in names or any(n.startswith("ppt/") for n in names):
                    return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                if "EPUB/content.opf" in names or "META-INF/container.xml" in names:
                    return "application/epub+zip"
        except Exception:
            pass
        return "application/zip"
    if head.startswith(b"BM") and len(data) > 26:
        return "image/bmp"
    return ""


def guess_mime_from_filename(filename: Optional[str]) -> str:
    if not filename:
        return ""
    ext = Path(filename).suffix.lower()
    if ext in EXT_TO_MIME:
        return EXT_TO_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return _clean_mime(guessed)


def classify_attachment_kind(mime_type: str, filename: str = "") -> str:
    mime = _clean_mime(mime_type) or guess_mime_from_filename(filename)
    if mime.startswith("image/") and mime != "image/svg+xml":
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or mime in OFFICE_MIMES or mime in {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/rtf",
        "application/epub+zip",
    }:
        return "document"
    if is_text_like_mime(mime, filename):
        return "text"
    return "file"


def is_text_like_mime(mime_type: str, filename: str = "") -> bool:
    mime = _clean_mime(mime_type) or guess_mime_from_filename(filename)
    if mime.startswith("text/") or mime in TEXT_LIKE_MIMES:
        return True
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in EXT_TO_MIME:
        mapped = EXT_TO_MIME[ext]
        return mapped.startswith("text/") or mapped in TEXT_LIKE_MIMES
    return False


def sanitize_filename(raw_name: Optional[str], mime_type: str = "", default_stem: str = "attachment") -> str:
    """Produce a safe basename with an extension matching `mime_type` when missing."""
    candidate = ""
    if raw_name:
        # Strip query/fragment and directory components (both POSIX and Windows separators)
        cleaned = str(raw_name).split("?", 1)[0].split("#", 1)[0].replace("\\", "/").strip()
        candidate = cleaned.rsplit("/", 1)[-1].strip()
        candidate = _SAFE_FILENAME_RE.sub("_", candidate).strip(" .")
    if not candidate:
        candidate = default_stem
    ext = Path(candidate).suffix.lower()
    if not ext:
        inferred_ext = MIME_TO_EXT.get(_clean_mime(mime_type), "")
        if not inferred_ext and mime_type:
            guessed_ext = mimetypes.guess_extension(_clean_mime(mime_type)) or ""
            inferred_ext = ".jpg" if guessed_ext == ".jpe" else guessed_ext
        if inferred_ext:
            candidate = f"{candidate}{inferred_ext}"
    if len(candidate) > 180:
        stem = Path(candidate).stem[:140]
        suffix = Path(candidate).suffix[:20]
        candidate = f"{stem}{suffix}"
    return candidate


def _decode_data_uri(uri: str) -> Tuple[bytes, str]:
    match = _DATA_URI_RE.match(uri.strip())
    if not match:
        raise ValueError("Invalid data URI")
    meta, payload = match.group(1), match.group(2)
    parts = [p.strip() for p in meta.split(";") if p.strip()]
    is_b64 = any(p.lower() == "base64" for p in parts)
    mime_parts = [p for p in parts if p.lower() != "base64" and "/" in p]
    mime_type = _clean_mime(mime_parts[0]) if mime_parts else ""
    if is_b64:
        compact = re.sub(r"\s+", "", payload)
        # Pad missing '=' if needed
        rem = len(compact) % 4
        if rem:
            compact += "=" * (4 - rem)
        raw = base64.b64decode(compact, validate=False)
    else:
        raw = urllib.parse.unquote_to_bytes(payload)
    if not mime_type:
        mime_type = sniff_mime_from_bytes(raw) or "application/octet-stream"
    return raw, mime_type


def _try_decode_base64(raw_str: str) -> Optional[bytes]:
    compact = re.sub(r"\s+", "", raw_str.strip())
    if not compact:
        return None
    rem = len(compact) % 4
    if rem == 1:
        return None
    if rem:
        compact += "=" * (4 - rem)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        try:
            return base64.urlsafe_b64decode(compact)
        except (binascii.Error, ValueError):
            return None


def _try_read_local_file(path_or_uri: str) -> Optional[Tuple[bytes, str, str]]:
    """Read an existing local file path or `file://` URI safely (blocking UNC shares & secret files)."""
    if os.getenv("BRIDGE_ALLOW_LOCAL_FILES", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    raw = path_or_uri.strip()
    if not raw:
        return None
    local_path: Optional[Path] = None
    if raw.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            # Block remote UNC file://host/share paths
            return None
        url_path = urllib.parse.unquote(parsed.path or "")
        if os.name == "nt" and len(url_path) >= 3 and url_path[0] == "/" and url_path[2] == ":":
            url_path = url_path[1:]
        local_path = Path(url_path)
    elif raw.startswith("\\\\"):
        # Block Windows UNC network shares
        return None
    elif (
        (len(raw) >= 3 and raw[1] == ":" and raw[2] in {"\\", "/"})
        or raw.startswith(("/", "./", "../", ".\\", "..\\"))
    ):
        local_path = Path(raw)

    if local_path is None:
        return None
    try:
        if not local_path.is_file():
            return None
        name_lower = local_path.name.lower()
        if name_lower in _BLOCKED_LOCAL_NAMES or name_lower.endswith(_BLOCKED_LOCAL_SUFFIXES):
            return None
        data = local_path.read_bytes()
        mime = guess_mime_from_filename(local_path.name) or sniff_mime_from_bytes(data) or "application/octet-stream"
        return data, mime, local_path.name
    except OSError:
        return None


@dataclass
class NormalizedAttachment:
    """Canonical representation of an image, audio, video, document, or general file attachment."""

    filename: str
    mime_type: str
    data: Optional[bytes] = None
    source_url: Optional[str] = None
    sha256: str = ""
    size_bytes: int = 0
    kind: str = "file"

    def __post_init__(self) -> None:
        self.refresh_metadata()

    @property
    def media_kind(self) -> str:
        return self.kind

    def refresh_metadata(self) -> None:
        mime = _clean_mime(self.mime_type)
        if self.data is not None:
            self.size_bytes = len(self.data)
            if not self.sha256:
                self.sha256 = hashlib.sha256(self.data).hexdigest()
            sniffed = sniff_mime_from_bytes(self.data)
            if not mime or mime == "application/octet-stream":
                mime = guess_mime_from_filename(self.filename) or sniffed or "application/octet-stream"
            elif mime == "application/zip" and sniffed in OFFICE_MIMES:
                mime = sniffed
        else:
            if not mime:
                mime = guess_mime_from_filename(self.filename) or "application/octet-stream"
            if not self.sha256 and self.source_url:
                self.sha256 = hashlib.sha256(self.source_url.encode("utf-8")).hexdigest()
        self.mime_type = mime
        default_stem = self.kind if self.kind in {"image", "audio", "video", "document"} else "attachment"
        self.filename = sanitize_filename(self.filename, self.mime_type, default_stem=default_stem)
        self.kind = classify_attachment_kind(self.mime_type, self.filename)

    def fingerprint_tag(self) -> str:
        """Compact deterministic tag used for multi-turn session prefix matching."""
        short_hash = (self.sha256 or "unknown")[:16]
        return f'[ATTACHMENT name="{self.filename}" mime="{self.mime_type}" sha256="{short_hash}"]'

    def history_note(self) -> str:
        """Human-readable marker placed in packed conversation transcripts."""
        return f"[Attached file: {self.filename} ({self.mime_type})]"

    async def async_resolve_bytes(self, timeout: float = 25.0) -> bytes:
        """Ensure binary bytes are loaded in memory (downloading HTTP/HTTPS URLs if needed)."""
        if self.data is not None:
            return self.data
        if not self.source_url:
            raise ValueError(f"Attachment '{self.filename}' has neither binary data nor source URL.")
        url = self.source_url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"Cannot download non-HTTP attachment URL '{url}' directly.")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
            hdr_mime = _clean_mime(resp.headers.get("content-type"))
            cd_header = resp.headers.get("content-disposition") or ""
            cd_match = _CONTENT_DISPOSITION_FILENAME_RE.search(cd_header)
            if cd_match and self.filename.startswith(("attachment", "image", "audio", "video", "document", "file")):
                raw_cd_name = urllib.parse.unquote(cd_match.group(1) or cd_match.group(2) or "")
                if raw_cd_name:
                    self.filename = raw_cd_name
            if hdr_mime and hdr_mime != "application/octet-stream":
                if not self.mime_type or self.mime_type == "application/octet-stream":
                    self.mime_type = hdr_mime
            self.data = raw
            self.sha256 = hashlib.sha256(raw).hexdigest()
            self.refresh_metadata()
            return raw

    def extract_text_fallback(self, max_chars: int = 120_000) -> Optional[str]:
        """Extract readable UTF-8 text from text/code files or OOXML (.docx/.xlsx/.pptx) documents."""
        if self.data is None:
            return None
        ext = Path(self.filename).suffix.lower()
        if self.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or ext == ".docx":
            text = extract_docx_text(self.data)
            if text is not None:
                return text[:max_chars]
        if self.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or ext == ".xlsx":
            text = extract_xlsx_text(self.data)
            if text is not None:
                return text[:max_chars]
        if self.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation" or ext == ".pptx":
            text = extract_pptx_text(self.data)
            if text is not None:
                return text[:max_chars]
        if is_text_like_mime(self.mime_type, self.filename):
            for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
                try:
                    decoded = self.data.decode(enc)
                    return decoded[:max_chars]
                except UnicodeDecodeError:
                    continue
        return None


def extract_docx_text(data: bytes) -> Optional[str]:
    """Extract paragraph and table cell text from a `.docx` archive using stdlib XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            if "word/document.xml" not in zf.namelist():
                return None
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        paragraphs: List[str] = []
        for elem in root.iter():
            local = elem.tag.rsplit("}", 1)[-1]
            if local == "p":
                runs: List[str] = []
                for child in elem.iter():
                    clocal = child.tag.rsplit("}", 1)[-1]
                    if clocal == "t" and child.text:
                        runs.append(child.text)
                    elif clocal == "tab":
                        runs.append("\t")
                    elif clocal in {"br", "cr"}:
                        runs.append("\n")
                line = "".join(runs).strip()
                if line:
                    paragraphs.append(line)
        return "\n".join(paragraphs)
    except Exception:
        return None


def extract_pptx_text(data: bytes) -> Optional[str]:
    """Extract slide text from a `.pptx` archive ordered by slide index."""
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            slide_names = [
                n for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            ]
            if not slide_names:
                return None

            def _slide_num(name: str) -> int:
                m = re.search(r"slide(\d+)\.xml$", name)
                return int(m.group(1)) if m else 0

            slide_names.sort(key=_slide_num)
            slides_out: List[str] = []
            for idx, sname in enumerate(slide_names, start=1):
                root = ET.fromstring(zf.read(sname))
                lines: List[str] = []
                for elem in root.iter():
                    if elem.tag.rsplit("}", 1)[-1] == "p":
                        chunks = [
                            c.text for c in elem.iter()
                            if c.tag.rsplit("}", 1)[-1] == "t" and c.text
                        ]
                        joined = "".join(chunks).strip()
                        if joined:
                            lines.append(joined)
                if lines:
                    slides_out.append(f"[Slide {idx}]\n" + "\n".join(lines))
        return "\n\n".join(slides_out)
    except Exception:
        return None


def extract_xlsx_text(data: bytes) -> Optional[str]:
    """Extract worksheet rows from an `.xlsx` archive into TSV-style readable sections."""
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            names = set(zf.namelist())
            shared_strings: List[str] = []
            if "xl/sharedStrings.xml" in names:
                ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in ss_root:
                    if si.tag.rsplit("}", 1)[-1] == "si":
                        parts = [
                            t.text for t in si.iter()
                            if t.tag.rsplit("}", 1)[-1] == "t" and t.text
                        ]
                        shared_strings.append("".join(parts))

            sheet_files = sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            if not sheet_files:
                return None

            sheets_out: List[str] = []
            for s_idx, sfile in enumerate(sheet_files, start=1):
                s_root = ET.fromstring(zf.read(sfile))
                rows_out: List[str] = []
                for row in s_root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    cells: List[str] = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        ctype = cell.attrib.get("t", "")
                        val_text = ""
                        if ctype == "inlineStr":
                            val_text = "".join(
                                t.text for t in cell.iter()
                                if t.tag.rsplit("}", 1)[-1] == "t" and t.text
                            )
                        else:
                            for child in cell:
                                if child.tag.rsplit("}", 1)[-1] == "v" and child.text is not None:
                                    val_text = child.text
                                    break
                            if ctype == "s" and val_text.isdigit():
                                idx = int(val_text)
                                if 0 <= idx < len(shared_strings):
                                    val_text = shared_strings[idx]
                        cells.append(val_text)
                    if any(c.strip() for c in cells):
                        rows_out.append("\t".join(cells))
                if rows_out:
                    sheets_out.append(f"[Sheet {s_idx}]\n" + "\n".join(rows_out))
            return "\n\n".join(sheets_out)
    except Exception:
        return None


def estimate_attachment_tokens(att: NormalizedAttachment) -> int:
    """Approximate multimodal token cost for prompt accounting."""
    if att.kind == "image":
        return 258
    if att.kind == "audio":
        return min(4096, max(64, (att.size_bytes or 32_000) // 500))
    if att.kind == "video":
        return min(8192, max(258, (att.size_bytes or 128_000) // 2000))
    extracted = att.extract_text_fallback(max_chars=80_000)
    if extracted:
        return max(16, len(extracted) // 4)
    if att.mime_type == "application/pdf":
        return min(4096, max(258, (att.size_bytes or 64_000) // 1500))
    return min(2048, max(64, (att.size_bytes or 4096) // 1024))


def deduplicate_attachments(attachments: Iterable[NormalizedAttachment]) -> List[NormalizedAttachment]:
    """Preserve attachment order while removing exact duplicate payloads within the same prompt."""
    seen: set[Tuple[str, str]] = set()
    result: List[NormalizedAttachment] = []
    for att in attachments:
        key = (att.sha256 or att.source_url or "", att.filename)
        if key in seen:
            continue
        seen.add(key)
        result.append(att)
    return result


def _attachment_from_url_or_data(
    raw_value: str,
    *,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
    default_kind: str = "file",
) -> Optional[NormalizedAttachment]:
    val = raw_value.strip()
    if not val:
        return None
    hint_mime = _clean_mime(mime_type) or guess_mime_from_filename(filename)

    # 1) Data URI (`data:<mime>;base64,...` or percent-encoded `data:<mime>,...`)
    if val.lower().startswith("data:"):
        raw_bytes, uri_mime = _decode_data_uri(val)
        eff_mime = hint_mime if (hint_mime and uri_mime == "application/octet-stream") else (uri_mime or hint_mime)
        return NormalizedAttachment(
            filename=filename or f"{default_kind}",
            mime_type=eff_mime,
            data=raw_bytes,
            kind=default_kind,
        )

    # 2) Remote HTTP(S) or Google Cloud Storage (`gs://`) URI
    if val.lower().startswith(("http://", "https://", "gs://")):
        parsed = urllib.parse.urlparse(val)
        url_name = urllib.parse.unquote(Path(parsed.path).name) if parsed.path else ""
        eff_name = filename or url_name or default_kind
        eff_mime = hint_mime or guess_mime_from_filename(eff_name) or (
            "image/png" if default_kind == "image" else "application/octet-stream"
        )
        return NormalizedAttachment(
            filename=eff_name,
            mime_type=eff_mime,
            data=None,
            source_url=val,
            kind=default_kind,
        )

    # 3) Local file path or `file://` URI (when enabled)
    local_hit = _try_read_local_file(val)
    if local_hit is not None:
        raw_bytes, local_mime, local_name = local_hit
        return NormalizedAttachment(
            filename=filename or local_name,
            mime_type=hint_mime or local_mime,
            data=raw_bytes,
            kind=default_kind,
        )

    # 4) Raw base64 string
    decoded = _try_decode_base64(val)
    if decoded is not None:
        sniffed = sniff_mime_from_bytes(decoded)
        eff_mime = hint_mime or sniffed or (
            "image/png" if default_kind == "image" else "application/octet-stream"
        )
        return NormalizedAttachment(
            filename=filename or default_kind,
            mime_type=eff_mime,
            data=decoded,
            kind=default_kind,
        )

    return None


def _parse_single_attachment_dict(
    item: Dict[str, Any],
    *,
    fallback_kind: str = "file",
) -> List[NormalizedAttachment]:
    """Normalize one multimodal content part or attachment dict into `NormalizedAttachment` items."""
    part_type = str(item.get("type") or fallback_kind).strip().lower()

    # Recurse into tool_result blocks that may carry images/documents alongside text
    if part_type == "tool_result":
        return extract_attachments_from_content(item.get("content"))

    if part_type in {"text", "input_text", "output_text", "refusal", "tool_use", "thinking"}:
        return []

    # 1) OpenAI `image_url` / `input_image` / `audio_url` / `video_url` / `input_video`
    for url_key, kind_hint in (
        ("image_url", "image"),
        ("input_image", "image"),
        ("audio_url", "audio"),
        ("video_url", "video"),
        ("input_video", "video"),
    ):
        if part_type == url_key or url_key in item:
            nested = item.get(url_key)
            if isinstance(nested, str):
                att = _attachment_from_url_or_data(
                    nested,
                    filename=item.get("filename") or item.get("name"),
                    mime_type=item.get("mime_type") or item.get("media_type"),
                    default_kind=kind_hint,
                )
                return [att] if att else []
            if isinstance(nested, dict):
                raw_u = (
                    nested.get("url")
                    or nested.get("file_data")
                    or nested.get("data")
                    or nested.get("path")
                    or ""
                )
                if isinstance(raw_u, str) and raw_u.strip():
                    att = _attachment_from_url_or_data(
                        raw_u,
                        filename=nested.get("filename") or nested.get("name") or item.get("filename") or item.get("name"),
                        mime_type=nested.get("mime_type") or nested.get("media_type") or item.get("mime_type"),
                        default_kind=kind_hint,
                    )
                    return [att] if att else []

    # 2) OpenAI `input_audio`: {"type": "input_audio", "input_audio": {"data": "...", "format": "wav"}}
    if part_type == "input_audio" or isinstance(item.get("input_audio"), dict):
        ia = item.get("input_audio") if isinstance(item.get("input_audio"), dict) else item
        raw_data = ia.get("data") or ia.get("url") or ia.get("file_data") or ""
        fmt = str(ia.get("format") or "").strip().lower().lstrip(".")
        audio_mime = (
            _clean_mime(ia.get("mime_type") or ia.get("media_type"))
            or AUDIO_FORMAT_TO_MIME.get(fmt, "")
            or (f"audio/{fmt}" if fmt else "audio/wav")
        )
        fname = ia.get("filename") or ia.get("name") or item.get("filename") or (
            f"audio.{fmt}" if fmt else "audio.wav"
        )
        if isinstance(raw_data, str) and raw_data.strip():
            att = _attachment_from_url_or_data(
                raw_data,
                filename=str(fname),
                mime_type=audio_mime,
                default_kind="audio",
            )
            return [att] if att else []

    # 3) OpenAI `file` / `input_file` blocks
    if part_type in {"file", "input_file"} or isinstance(item.get("file"), dict) or isinstance(item.get("input_file"), dict):
        fobj = (
            item.get("file")
            if isinstance(item.get("file"), dict)
            else item.get("input_file")
            if isinstance(item.get("input_file"), dict)
            else item
        )
        fname = (
            fobj.get("filename")
            or fobj.get("name")
            or fobj.get("title")
            or item.get("filename")
            or item.get("name")
            or item.get("title")
        )
        fmime = (
            fobj.get("mime_type")
            or fobj.get("media_type")
            or fobj.get("content_type")
            or item.get("mime_type")
            or item.get("media_type")
            or item.get("content_type")
        )
        for bin_key in ("file_data", "data", "base64", "url", "file_url", "path", "uri"):
            raw_val = fobj.get(bin_key) if fobj.get(bin_key) is not None else item.get(bin_key)
            if isinstance(raw_val, str) and raw_val.strip():
                att = _attachment_from_url_or_data(
                    raw_val,
                    filename=str(fname) if fname else None,
                    mime_type=str(fmime) if fmime else None,
                    default_kind="file",
                )
                if att:
                    return [att]
        # Plain-text file content (`content` or `text`)
        for txt_key in ("text", "content"):
            txt_val = fobj.get(txt_key) if fobj.get(txt_key) is not None else item.get(txt_key)
            if isinstance(txt_val, str) and txt_val:
                eff_name = str(fname) if fname else "document.txt"
                eff_mime = _clean_mime(str(fmime) if fmime else "") or guess_mime_from_filename(eff_name) or "text/plain"
                return [
                    NormalizedAttachment(
                        filename=eff_name,
                        mime_type=eff_mime,
                        data=txt_val.encode("utf-8"),
                        kind="text",
                    )
                ]

    # 4) Anthropic / generic `image` | `document` | `audio` | `video` | `attachment` blocks
    if part_type in {"image", "document", "audio", "video", "attachment", "media"}:
        kind_map = {
            "image": "image",
            "audio": "audio",
            "video": "video",
            "document": "document",
        }
        def_kind = kind_map.get(part_type, "file")
        fname = item.get("filename") or item.get("name") or item.get("title")
        fmime = item.get("mime_type") or item.get("media_type") or item.get("content_type")
        src = item.get("source")
        if isinstance(src, dict):
            src_type = str(src.get("type") or "").strip().lower()
            src_mime = src.get("media_type") or src.get("mime_type") or fmime
            if src_type == "text" and isinstance(src.get("data"), str):
                eff_name = str(fname) if fname else "document.txt"
                eff_mime = _clean_mime(str(src_mime) if src_mime else "") or guess_mime_from_filename(eff_name) or "text/plain"
                return [
                    NormalizedAttachment(
                        filename=eff_name,
                        mime_type=eff_mime,
                        data=src["data"].encode("utf-8"),
                        kind="text",
                    )
                ]
            if src_type == "content" and isinstance(src.get("content"), list):
                text_chunks = [
                    b.get("text", "")
                    for b in src["content"]
                    if isinstance(b, dict) and isinstance(b.get("text"), str)
                ]
                joined = "\n".join(c for c in text_chunks if c)
                if joined:
                    eff_name = str(fname) if fname else "document.txt"
                    return [
                        NormalizedAttachment(
                            filename=eff_name,
                            mime_type="text/plain",
                            data=joined.encode("utf-8"),
                            kind="text",
                        )
                    ]
            raw_src = src.get("data") or src.get("url") or src.get("uri") or src.get("path") or ""
            if isinstance(raw_src, str) and raw_src.strip():
                att = _attachment_from_url_or_data(
                    raw_src,
                    filename=str(fname) if fname else None,
                    mime_type=str(src_mime) if src_mime else None,
                    default_kind=def_kind,
                )
                if att:
                    return [att]

        for key in ("data", "file_data", "base64", "url", "uri", "path"):
            raw_val = item.get(key)
            if isinstance(raw_val, str) and raw_val.strip():
                att = _attachment_from_url_or_data(
                    raw_val,
                    filename=str(fname) if fname else None,
                    mime_type=str(fmime) if fmime else None,
                    default_kind=def_kind,
                )
                if att:
                    return [att]

        if isinstance(item.get("text"), str) and item["text"]:
            eff_name = str(fname) if fname else "document.txt"
            eff_mime = _clean_mime(str(fmime) if fmime else "") or guess_mime_from_filename(eff_name) or "text/plain"
            return [
                NormalizedAttachment(
                    filename=eff_name,
                    mime_type=eff_mime,
                    data=item["text"].encode("utf-8"),
                    kind="text",
                )
            ]

    return []


def extract_attachments_from_content(
    content: Any,
    *,
    extra_images: Optional[Sequence[Any]] = None,
    extra_files: Optional[Sequence[Any]] = None,
    extra_audio: Optional[Dict[str, Any]] = None,
) -> List[NormalizedAttachment]:
    """Extract normalized attachments from OpenAI/Anthropic/Ollama multimodal message structures."""
    collected: List[NormalizedAttachment] = []

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                collected.extend(_parse_single_attachment_dict(item))

    if extra_images:
        for img in extra_images:
            if isinstance(img, str) and img.strip():
                att = _attachment_from_url_or_data(img, default_kind="image")
                if att:
                    collected.append(att)
            elif isinstance(img, dict):
                collected.extend(_parse_single_attachment_dict(img, fallback_kind="image"))

    if extra_files:
        for fitem in extra_files:
            if isinstance(fitem, str) and fitem.strip():
                att = _attachment_from_url_or_data(fitem, default_kind="file")
                if att:
                    collected.append(att)
            elif isinstance(fitem, dict):
                collected.extend(_parse_single_attachment_dict(fitem, fallback_kind="file"))

    if isinstance(extra_audio, dict) and extra_audio:
        collected.extend(_parse_single_attachment_dict({"type": "input_audio", "input_audio": extra_audio}))
    return collected


extract_message_attachments = extract_attachments_from_content

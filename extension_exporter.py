"""Pure-Python exporter creating an isolated `extension/` bundle (with PNG icons) and `userscript/`."""

from __future__ import annotations

import json
import shutil
import struct
import zlib
from pathlib import Path
from typing import Tuple


def _make_png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def render_spark_icon_png(size: int) -> bytes:
    """Render a valid RGBA PNG icon (blue-indigo rounded badge with a 4-point Gemini spark)."""
    rows = bytearray()
    center = (size - 1) / 2.0
    radius = size * 0.46
    corner_r = size * 0.22
    half_box = size * 0.44

    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None)
        dy = abs(y - center)
        ny = dy / max(radius, 1.0)
        for x in range(size):
            dx = abs(x - center)
            nx = dx / max(radius, 1.0)

            # Rounded square signed distance
            qx = max(dx - (half_box - corner_r), 0.0)
            qy = max(dy - (half_box - corner_r), 0.0)
            dist = (qx * qx + qy * qy) ** 0.5 - corner_r

            if dist > 0.6:
                rows.extend((0, 0, 0, 0))
                continue

            alpha = 255 if dist < -0.5 else int(max(0.0, min(1.0, 0.5 - dist)) * 255)

            # Diagonal gradient (#2563EB -> #7C3AED)
            t = (x + y) / max(2.0 * (size - 1), 1.0)
            r = int(37 * (1.0 - t) + 124 * t)
            g = int(99 * (1.0 - t) + 58 * t)
            b = int(235 * (1.0 - t) + 237 * t)

            # 4-point star / spark curve: (nx^0.6 + ny^0.6) <= 0.62
            spark = (nx ** 0.58 + ny ** 0.58) <= 0.62
            if spark:
                r, g, b = 248, 250, 255

            rows.extend((r, g, b, alpha))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _make_png_chunk(b"IHDR", ihdr)
        + _make_png_chunk(b"IDAT", compressed)
        + _make_png_chunk(b"IEND", b"")
    )


EXTENSION_RUNTIME_FILES: Tuple[str, ...] = (
    "background.js",
    "content-bridge.js",
    "page-driver.js",
    "popup.html",
    "popup.css",
    "popup.js",
)


def export_extension_bundle(root_dir: Path | None = None) -> Path:
    """Materialize `extension/` (with real 16/48/128 PNG icons) and `userscript/` from root sources."""
    root = root_dir or Path(__file__).resolve().parent
    manifest_src = root / "manifest.json"
    if not manifest_src.is_file():
        return root

    ext_dir = root / "extension"
    icons_dir = ext_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    for px in (16, 48, 128):
        icon_file = icons_dir / f"icon-{px}.png"
        if not icon_file.is_file():
            icon_file.write_bytes(render_spark_icon_png(px))

    for filename in EXTENSION_RUNTIME_FILES:
        src = root / filename
        if src.is_file():
            shutil.copy2(src, ext_dir / filename)

    manifest_data = json.loads(manifest_src.read_text(encoding="utf-8"))
    icon_map = {
        "16": "icons/icon-16.png",
        "48": "icons/icon-48.png",
        "128": "icons/icon-128.png",
    }
    manifest_data["icons"] = icon_map
    if isinstance(manifest_data.get("action"), dict):
        manifest_data["action"]["default_icon"] = icon_map

    (ext_dir / "manifest.json").write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    userscript_src = root / "gemini-enterprise-bridge.user.js"
    if userscript_src.is_file():
        us_dir = root / "userscript"
        us_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(userscript_src, us_dir / "gemini-enterprise-bridge.user.js")

    return ext_dir


if __name__ == "__main__":
    target = export_extension_bundle()
    print(f"Exported Chrome/Edge MV3 extension bundle to: {target}")

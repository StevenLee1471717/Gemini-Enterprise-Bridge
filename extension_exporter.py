"""Pure-Python exporter & deterministic GitHub Release packager for `extension/`, `userscript/`, and `dist/`."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from version import APP_VERSION, get_github_repo, get_release_download_base


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


def _resolve_root_version(root: Path) -> str:
    version_file = root / "version.py"
    if version_file.is_file():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("APP_VERSION") and "=" in stripped:
                rhs = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if rhs:
                    return rhs
    return APP_VERSION


def export_extension_bundle(
    root_dir: Path | None = None,
    version: str | None = None,
    repo: str | None = None,
) -> Path:
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

    eff_repo = (repo or get_github_repo()).strip().strip("/")
    eff_version = (version or _resolve_root_version(root)).strip()
    manifest_data = json.loads(manifest_src.read_text(encoding="utf-8"))
    manifest_data["version"] = eff_version
    manifest_data["homepage_url"] = f"https://github.com/{eff_repo}"
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


BACKEND_EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".impeccable",
    ".vscode",
    ".idea",
    "dist",
    "build",
    "logs",
    "generated",
    ".cache",
}

BACKEND_EXCLUDED_FILES = {
    ".env",
    "usage_stats.json",
    "usage_stats.json.tmp",
    ".DS_Store",
    "Thumbs.db",
}

BACKEND_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    ".pem",
    ".key",
)

DETERMINISTIC_ZIP_DT = (2026, 1, 1, 0, 0, 0)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_deterministic_zip(zip_path: Path, entries: Iterable[Tuple[str, Path]]) -> str:
    """Write sorted, reproducible ZIP entries and return SHA-256 hex digest."""
    sorted_entries = sorted(entries, key=lambda item: item[0])
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, src_path in sorted_entries:
            norm_arc = arcname.replace("\\", "/").lstrip("/")
            info = zipfile.ZipInfo(filename=norm_arc, date_time=DETERMINISTIC_ZIP_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, src_path.read_bytes())
    return _sha256_file(zip_path)


def _should_exclude_from_backend_bundle(rel_path: Path) -> bool:
    parts = rel_path.parts
    if any(part in BACKEND_EXCLUDED_DIRS for part in parts):
        return True
    name = rel_path.name
    if name in BACKEND_EXCLUDED_FILES:
        return True
    if name.startswith(".env.") and name != "env.example":
        return True
    if name.endswith(BACKEND_EXCLUDED_SUFFIXES):
        return True
    return False


def build_update_manifest_xml(version: str, download_base: str) -> str:
    """Build Chrome/Edge Enterprise gupdate XML manifest pointing to GitHub Releases."""
    ext_zip_url = f"{download_base}/gemini-enterprise-bridge-extension.zip"
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        "  <app appid='gemini-enterprise-bridge'>\n"
        f"    <updatecheck codebase='{ext_zip_url}' version='{version}' />\n"
        "  </app>\n"
        "</gupdate>\n"
    )


def build_release_artifacts(
    dist_dir: Path | None = None,
    root_dir: Path | None = None,
    version: str | None = None,
    repo: str | None = None,
) -> Dict[str, Any]:
    """Export `extension/` + `userscript/` and build release assets in `dist/`."""
    root = (root_dir or Path(__file__).resolve().parent).resolve()
    target_dist = (dist_dir or (root / "dist")).resolve()
    eff_version = (version or APP_VERSION).strip()
    eff_repo = (repo or get_github_repo()).strip().strip("/")
    download_base = get_release_download_base(eff_repo)

    ext_dir = export_extension_bundle(root, version=eff_version, repo=eff_repo)
    target_dist.mkdir(parents=True, exist_ok=True)

    # 1. Clean unpacked extension zip (files at archive root for direct extraction into `extension/`)
    ext_entries = [
        (p.relative_to(ext_dir).as_posix(), p)
        for p in ext_dir.rglob("*")
        if p.is_file()
    ]
    ext_zip_path = target_dist / "gemini-enterprise-bridge-extension.zip"
    ext_sha256 = _write_deterministic_zip(ext_zip_path, ext_entries)

    # 2. Full backend + extension + launcher zip (strictly excluding .env, usage_stats.json, .venv, __pycache__)
    backend_entries = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if target_dist in p.parents or p == target_dist:
            continue
        rel = p.relative_to(root)
        if _should_exclude_from_backend_bundle(rel):
            continue
        backend_entries.append((rel.as_posix(), p))
    backend_zip_path = target_dist / "gemini-enterprise-bridge-backend.zip"
    backend_sha256 = _write_deterministic_zip(backend_zip_path, backend_entries)

    # 3. CSP-immune Tampermonkey userscript
    userscript_src = root / "gemini-enterprise-bridge.user.js"
    userscript_dist = target_dist / "gemini-enterprise-bridge.user.js"
    userscript_sha256 = ""
    if userscript_src.is_file():
        shutil.copy2(userscript_src, userscript_dist)
        userscript_sha256 = _sha256_file(userscript_dist)

    # 4. Chrome/Edge gupdate XML manifest
    update_xml_path = target_dist / "update-manifest.xml"
    update_xml_path.write_text(
        build_update_manifest_xml(eff_version, download_base),
        encoding="utf-8",
    )

    # 5. Structured release-manifest.json consumed by updater.py and background.js
    published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    release_manifest: Dict[str, Any] = {
        "version": eff_version,
        "tag": f"v{eff_version}",
        "published_at": published_at,
        "repo": eff_repo,
        "release_url": f"https://github.com/{eff_repo}/releases/tag/v{eff_version}",
        "extension_zip_url": f"{download_base}/gemini-enterprise-bridge-extension.zip",
        "extension_zip_sha256": ext_sha256,
        "backend_zip_url": f"{download_base}/gemini-enterprise-bridge-backend.zip",
        "backend_zip_sha256": backend_sha256,
        "userscript_url": f"{download_base}/gemini-enterprise-bridge.user.js",
        "userscript_sha256": userscript_sha256,
        "update_manifest_xml_url": f"{download_base}/update-manifest.xml",
    }
    manifest_json_path = target_dist / "release-manifest.json"
    manifest_json_path.write_text(
        json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return release_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Chrome/Edge MV3 extension bundle and optional GitHub Release packages."
    )
    parser.add_argument(
        "--package",
        action="store_true",
        help="Also build deterministic GitHub Release artifacts in dist/",
    )
    parser.add_argument(
        "--dist-dir",
        type=str,
        default="",
        help="Custom output directory for --package (default: <root>/dist)",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    target = export_extension_bundle(root)
    print(f"Exported Chrome/Edge MV3 extension bundle (v{APP_VERSION}) to: {target}")
    if args.package:
        dist_target = Path(args.dist_dir).resolve() if args.dist_dir else (root / "dist")
        manifest = build_release_artifacts(dist_dir=dist_target, root_dir=root)
        print(
            f"Built release artifacts in {dist_target} "
            f"(tag={manifest['tag']}, ext_sha256={manifest['extension_zip_sha256'][:12]}..., "
            f"backend_sha256={manifest['backend_zip_sha256'][:12]}...)"
        )


if __name__ == "__main__":
    main()

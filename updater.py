"""Zero-dependency GitHub Releases Auto-Updater for Backend & Unpacked Chrome/Edge Extension."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from extension_exporter import export_extension_bundle
from version import (
    APP_VERSION,
    get_github_repo,
    get_release_download_base,
    get_releases_latest_api,
)

logger = logging.getLogger("gemini_bridge.updater")

EXIT_RESTART_FOR_UPDATE = 42
DEFAULT_CHECK_TTL_SECONDS = 600.0

PROTECTED_USER_FILES = {
    ".env",
    "usage_stats.json",
    "usage_stats.json.tmp",
}


class UpdateError(RuntimeError):
    """Raised when downloading or applying a release update fails."""


class UpdateIntegrityError(UpdateError):
    """Raised when a downloaded release bundle fails SHA-256 verification."""


_SEMVER_RE = re.compile(
    r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def parse_semver(raw: str | None) -> Tuple[int, int, int, int, str]:
    """Parse semantic version into comparable tuple; stable releases sort above pre-releases."""
    text = (raw or "").strip()
    if not text:
        return (0, 0, 0, 0, "")
    match = _SEMVER_RE.match(text)
    if not match:
        nums = [int(p) for p in re.findall(r"\d+", text)[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], 1, "")
    major = int(match.group(1) or 0)
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    prerelease = match.group(4) or ""
    is_stable = 1 if not prerelease else 0
    return (major, minor, patch, is_stable, prerelease)


def compare_semver(v1: str | None, v2: str | None) -> int:
    """Return -1 if v1 < v2, 0 if equal, +1 if v1 > v2."""
    t1 = parse_semver(v1)
    t2 = parse_semver(v2)
    if t1[:4] != t2[:4]:
        return 1 if t1[:4] > t2[:4] else -1
    if t1[4] == t2[4]:
        return 0
    return 1 if t1[4] > t2[4] else -1


def normalize_version(raw: str | None) -> str:
    text = (raw or "").strip()
    if text.lower().startswith("v") and len(text) > 1 and text[1].isdigit():
        return text[1:]
    return text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected_sha256: str | None) -> str:
    actual = sha256_file(path)
    expected = (expected_sha256 or "").strip().lower()
    if expected and actual.lower() != expected:
        raise UpdateIntegrityError(
            f"SHA-256 校验失败 ({path.name}): 期望 {expected}，实际 {actual}"
        )
    return actual


def _get_github_token() -> str:
    return (
        os.getenv("BRIDGE_GITHUB_TOKEN", "").strip()
        or os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GH_TOKEN", "").strip()
    )


def _default_url_fetcher(url: str, timeout: float = 12.0, accept: str = "*/*") -> bytes:
    headers = {
        "User-Agent": f"Gemini-Enterprise-Bridge-Updater/{APP_VERSION}",
        "Accept": accept,
    }
    token = _get_github_token()
    if token and ("github.com" in url or "githubusercontent.com" in url):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _gh_cli_available() -> bool:
    return shutil.which("gh") is not None


def _fetch_latest_via_gh_cli(repo: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """Fallback for private GitHub repositories when local `gh` CLI is authenticated."""
    if not _gh_cli_available():
        return None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/releases/latest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _download_asset_via_gh_cli(
    repo: str, asset_name: str, dest_file: Path, tag: Optional[str] = None, timeout: float = 45.0
) -> bool:
    if not _gh_cli_available():
        return False
    cmd = ["gh", "release", "download"]
    if tag:
        cmd.append(tag)
    cmd.extend(["--repo", repo, "--pattern", asset_name, "--output", str(dest_file), "--clobber"])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0 and dest_file.is_file() and dest_file.stat().st_size > 0
    except Exception:
        return False


def _safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    preserve_relative_paths: Optional[set[str]] = None,
) -> list[str]:
    """Safely extract zip into dest_dir while preventing ZipSlip and preserving user config/ledger."""
    preserved = preserve_relative_paths or set()
    dest_resolved = dest_dir.resolve()
    extracted_files: list[str] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            rel_norm = member.filename.replace("\\", "/").lstrip("/")
            if not rel_norm or ".." in Path(rel_norm).parts:
                raise UpdateError(f"拒绝解压不安全的归档路径: {member.filename}")
            if rel_norm in preserved or Path(rel_norm).name in PROTECTED_USER_FILES:
                if (dest_resolved / rel_norm).exists():
                    continue
            target_path = (dest_resolved / rel_norm).resolve()
            if dest_resolved != target_path and dest_resolved not in target_path.parents:
                raise UpdateError(f"归档路径越界: {member.filename}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, target_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_files.append(rel_norm)
    return extracted_files


class UpdateManager:
    """Coordinates GitHub Release polling, SHA-256 verified bundle updates, and version state."""

    def __init__(
        self,
        root_dir: Optional[Path] = None,
        current_version: str = APP_VERSION,
        repo: Optional[str] = None,
        check_ttl_seconds: float = DEFAULT_CHECK_TTL_SECONDS,
        url_fetcher: Optional[Callable[[str, float, str], bytes]] = None,
    ) -> None:
        self.root_dir = (root_dir or Path(__file__).resolve().parent).resolve()
        self.current_version = normalize_version(current_version) or APP_VERSION
        self.repo = (repo or get_github_repo()).strip().strip("/")
        self.check_ttl_seconds = max(0.0, float(check_ttl_seconds))
        self._url_fetcher = url_fetcher or _default_url_fetcher
        self._cached_check: Optional[Dict[str, Any]] = None
        self._last_check_mono: float = 0.0
        self.connected_extension_version: str = ""
        self._warned_backend_outdated_for: str = ""

    def record_extension_version(self, ext_version: str | None) -> bool:
        """Record version reported by Chrome/Edge extension; returns True if extension > backend."""
        cleaned = normalize_version(ext_version)
        if not cleaned:
            return False
        self.connected_extension_version = cleaned
        if compare_semver(cleaned, self.current_version) > 0:
            if self._warned_backend_outdated_for != cleaned:
                self._warned_backend_outdated_for = cleaned
                logger.warning(
                    "检测到浏览器扩展已升级至 v%s（当前本地后端 v%s），建议立即更新后端（控制台或插件弹窗点击『立即自动更新后端』）。",
                    cleaned,
                    self.current_version,
                )
            return True
        return False

    def _parse_github_release_api_json(self, api_data: Dict[str, Any]) -> Dict[str, Any]:
        tag_name = str(api_data.get("tag_name") or "").strip()
        latest_ver = normalize_version(tag_name or str(api_data.get("name") or ""))
        download_base = get_release_download_base(self.repo)
        assets = api_data.get("assets") if isinstance(api_data.get("assets"), list) else []
        asset_urls: Dict[str, str] = {}
        asset_digests: Dict[str, str] = {}
        for item in assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            b_url = str(item.get("browser_download_url") or "")
            if name and b_url:
                asset_urls[name] = b_url
            digest_raw = str(item.get("digest") or "")
            if digest_raw.startswith("sha256:"):
                asset_digests[name] = digest_raw.split(":", 1)[1].strip()

        return {
            "version": latest_ver,
            "tag": tag_name or (f"v{latest_ver}" if latest_ver else ""),
            "published_at": str(api_data.get("published_at") or ""),
            "repo": self.repo,
            "release_url": str(
                api_data.get("html_url") or f"https://github.com/{self.repo}/releases/latest"
            ),
            "extension_zip_url": asset_urls.get(
                "gemini-enterprise-bridge-extension.zip",
                f"{download_base}/gemini-enterprise-bridge-extension.zip",
            ),
            "extension_zip_sha256": asset_digests.get("gemini-enterprise-bridge-extension.zip", ""),
            "backend_zip_url": asset_urls.get(
                "gemini-enterprise-bridge-backend.zip",
                f"{download_base}/gemini-enterprise-bridge-backend.zip",
            ),
            "backend_zip_sha256": asset_digests.get("gemini-enterprise-bridge-backend.zip", ""),
            "userscript_url": asset_urls.get(
                "gemini-enterprise-bridge.user.js",
                f"{download_base}/gemini-enterprise-bridge.user.js",
            ),
        }

    def check_github_update(self, force: bool = False, timeout: float = 8.0) -> Dict[str, Any]:
        """Query GitHub Releases for latest version; cached and resilient against offline/rate-limit."""
        now_mono = time.monotonic()
        if (
            not force
            and self._cached_check is not None
            and (now_mono - self._last_check_mono) < self.check_ttl_seconds
        ):
            return self._enrich_status(dict(self._cached_check))

        download_base = get_release_download_base(self.repo)
        manifest_url = f"{download_base}/release-manifest.json"
        api_url = get_releases_latest_api(self.repo)
        manifest_data: Optional[Dict[str, Any]] = None
        last_err = ""

        # 1. Fast path: release-manifest.json from GitHub Releases latest download
        try:
            raw = self._url_fetcher(manifest_url, timeout, "application/json")
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict) and parsed.get("version"):
                manifest_data = parsed
        except Exception as exc:
            last_err = str(exc)

        # 2. Fallback: GitHub REST API /releases/latest
        if manifest_data is None:
            try:
                raw_api = self._url_fetcher(api_url, timeout, "application/vnd.github+json")
                parsed_api = json.loads(raw_api.decode("utf-8"))
                if isinstance(parsed_api, dict) and parsed_api.get("tag_name"):
                    manifest_data = self._parse_github_release_api_json(parsed_api)
            except Exception as exc:
                last_err = str(exc)

        # 3. Private repo local fallback: authenticated `gh` CLI
        if manifest_data is None and self._url_fetcher is _default_url_fetcher:
            gh_api_data = _fetch_latest_via_gh_cli(self.repo, timeout=timeout)
            if gh_api_data and gh_api_data.get("tag_name"):
                manifest_data = self._parse_github_release_api_json(gh_api_data)
                # Try reading release-manifest.json via gh release download for exact SHA-256s
                with tempfile.TemporaryDirectory(prefix="geb_gh_manifest_") as tmp:
                    mf_file = Path(tmp) / "release-manifest.json"
                    if _download_asset_via_gh_cli(
                        self.repo,
                        "release-manifest.json",
                        mf_file,
                        tag=str(gh_api_data.get("tag_name") or ""),
                        timeout=timeout,
                    ):
                        try:
                            loaded = json.loads(mf_file.read_text(encoding="utf-8"))
                            if isinstance(loaded, dict) and loaded.get("version"):
                                manifest_data = loaded
                        except Exception:
                            pass

        checked_ts = int(time.time())
        if manifest_data is None:
            result: Dict[str, Any] = {
                "ok": False,
                "repo": self.repo,
                "latest_github_version": "",
                "release_url": f"https://github.com/{self.repo}/releases",
                "published_at": "",
                "download_urls": {
                    "extension_zip": f"{download_base}/gemini-enterprise-bridge-extension.zip",
                    "backend_zip": f"{download_base}/gemini-enterprise-bridge-backend.zip",
                    "userscript": f"{download_base}/gemini-enterprise-bridge.user.js",
                },
                "checksums": {},
                "last_checked_at": checked_ts,
                "error": last_err or "未找到已发布的 GitHub Release",
            }
        else:
            latest_ver = normalize_version(str(manifest_data.get("version") or ""))
            result = {
                "ok": True,
                "repo": self.repo,
                "latest_github_version": latest_ver,
                "tag": str(manifest_data.get("tag") or f"v{latest_ver}"),
                "release_url": str(
                    manifest_data.get("release_url")
                    or f"https://github.com/{self.repo}/releases/tag/v{latest_ver}"
                ),
                "published_at": str(manifest_data.get("published_at") or ""),
                "download_urls": {
                    "extension_zip": str(
                        manifest_data.get("extension_zip_url")
                        or f"{download_base}/gemini-enterprise-bridge-extension.zip"
                    ),
                    "backend_zip": str(
                        manifest_data.get("backend_zip_url")
                        or f"{download_base}/gemini-enterprise-bridge-backend.zip"
                    ),
                    "userscript": str(
                        manifest_data.get("userscript_url")
                        or f"{download_base}/gemini-enterprise-bridge.user.js"
                    ),
                },
                "checksums": {
                    "extension_zip_sha256": str(manifest_data.get("extension_zip_sha256") or ""),
                    "backend_zip_sha256": str(manifest_data.get("backend_zip_sha256") or ""),
                    "userscript_sha256": str(manifest_data.get("userscript_sha256") or ""),
                },
                "last_checked_at": checked_ts,
                "error": "",
            }

        self._cached_check = dict(result)
        self._last_check_mono = now_mono
        return self._enrich_status(result)

    def status_snapshot(self, include_remote_if_cached: bool = True) -> Dict[str, Any]:
        """Return lightweight status snapshot without blocking on network IO."""
        base: Dict[str, Any]
        if include_remote_if_cached and self._cached_check is not None:
            base = dict(self._cached_check)
        else:
            download_base = get_release_download_base(self.repo)
            base = {
                "ok": True,
                "repo": self.repo,
                "latest_github_version": "",
                "release_url": f"https://github.com/{self.repo}/releases",
                "published_at": "",
                "download_urls": {
                    "extension_zip": f"{download_base}/gemini-enterprise-bridge-extension.zip",
                    "backend_zip": f"{download_base}/gemini-enterprise-bridge-backend.zip",
                    "userscript": f"{download_base}/gemini-enterprise-bridge.user.js",
                },
                "checksums": {},
                "last_checked_at": 0,
                "error": "",
            }
        return self._enrich_status(base)

    def _enrich_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ext_ver = self.connected_extension_version
        remote_ver = normalize_version(str(data.get("latest_github_version") or ""))
        ext_newer_than_backend = bool(
            ext_ver and compare_semver(ext_ver, self.current_version) > 0
        )
        remote_newer_than_backend = bool(
            remote_ver and compare_semver(remote_ver, self.current_version) > 0
        )
        backend_outdated = ext_newer_than_backend or remote_newer_than_backend

        extension_outdated = False
        if ext_ver:
            if compare_semver(self.current_version, ext_ver) > 0:
                extension_outdated = True
            elif remote_ver and compare_semver(remote_ver, ext_ver) > 0:
                extension_outdated = True
        elif remote_ver and compare_semver(remote_ver, self.current_version) > 0:
            extension_outdated = True

        highest_known = self.current_version
        for candidate in (ext_ver, remote_ver):
            if candidate and compare_semver(candidate, highest_known) > 0:
                highest_known = candidate

        out = dict(data)
        out["current_backend_version"] = self.current_version
        out["connected_extension_version"] = ext_ver
        out["highest_known_version"] = highest_known
        out["backend_outdated"] = backend_outdated
        out["backend_update_required"] = backend_outdated
        out["extension_outdated"] = extension_outdated
        out["update_available"] = backend_outdated or extension_outdated
        return out

    def _download_verified_asset(
        self,
        url: str,
        asset_name: str,
        expected_sha256: str,
        dest_path: Path,
        tag: str = "",
        timeout: float = 45.0,
    ) -> str:
        downloaded = False
        last_exc: Optional[Exception] = None
        try:
            payload = self._url_fetcher(url, timeout, "application/octet-stream")
            dest_path.write_bytes(payload)
            downloaded = True
        except Exception as exc:
            last_exc = exc

        if not downloaded and self._url_fetcher is _default_url_fetcher:
            downloaded = _download_asset_via_gh_cli(
                self.repo, asset_name, dest_path, tag=tag or None, timeout=timeout
            )

        if not downloaded or not dest_path.is_file():
            raise UpdateError(f"无法从 GitHub 下载更新包 {asset_name}: {last_exc or 'download_failed'}")

        return verify_sha256(dest_path, expected_sha256)

    def update_extension_only(
        self, manifest_override: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Download `gemini-enterprise-bridge-extension.zip`, verify SHA-256, and update `extension/`."""
        info = manifest_override or self.check_github_update(force=True)
        if not info.get("ok") and not manifest_override:
            raise UpdateError(info.get("error") or "无法获取 GitHub Release 元数据")

        dl_urls = info.get("download_urls") or {}
        checksums = info.get("checksums") or {}
        ext_url = str(
            info.get("extension_zip_url") or dl_urls.get("extension_zip") or ""
        ).strip()
        ext_sha = str(
            info.get("extension_zip_sha256") or checksums.get("extension_zip_sha256") or ""
        ).strip()
        us_url = str(info.get("userscript_url") or dl_urls.get("userscript") or "").strip()
        us_sha = str(
            info.get("userscript_sha256") or checksums.get("userscript_sha256") or ""
        ).strip()
        tag = str(info.get("tag") or "")

        if not ext_url:
            raise UpdateError("缺少 extension_zip_url 下载地址")

        with tempfile.TemporaryDirectory(prefix="geb_ext_upd_") as tmp:
            tmp_dir = Path(tmp)
            zip_file = tmp_dir / "gemini-enterprise-bridge-extension.zip"
            verified_sha = self._download_verified_asset(
                ext_url,
                "gemini-enterprise-bridge-extension.zip",
                ext_sha,
                zip_file,
                tag=tag,
            )
            stage_dir = tmp_dir / "stage"
            stage_dir.mkdir(parents=True, exist_ok=True)
            extracted = _safe_extract_zip(zip_file, stage_dir)

            ext_dir = self.root_dir / "extension"
            ext_dir.mkdir(parents=True, exist_ok=True)
            for src_file in stage_dir.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(stage_dir)
                    dst = ext_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst)

            if us_url:
                try:
                    us_tmp = tmp_dir / "gemini-enterprise-bridge.user.js"
                    self._download_verified_asset(
                        us_url,
                        "gemini-enterprise-bridge.user.js",
                        us_sha,
                        us_tmp,
                        tag=tag,
                    )
                    us_dir = self.root_dir / "userscript"
                    us_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(us_tmp, us_dir / "gemini-enterprise-bridge.user.js")
                except UpdateIntegrityError:
                    raise
                except Exception:
                    pass

        updated_ver = normalize_version(
            str(info.get("latest_github_version") or info.get("version") or "")
        )
        manifest_on_disk = self.root_dir / "extension" / "manifest.json"
        if manifest_on_disk.is_file():
            try:
                m_json = json.loads(manifest_on_disk.read_text(encoding="utf-8"))
                if m_json.get("version"):
                    updated_ver = normalize_version(str(m_json["version"]))
            except Exception:
                pass

        return {
            "ok": True,
            "updated_version": updated_ver,
            "sha256": verified_sha,
            "files_updated": len(extracted),
            "extension_dir": str(self.root_dir / "extension"),
        }

    def _try_git_fast_forward(self, target_tag: str = "") -> Optional[Dict[str, Any]]:
        git_dir = self.root_dir / ".git"
        if not git_dir.exists() or shutil.which("git") is None:
            return None
        try:
            status_proc = subprocess.run(
                ["git", "--no-pager", "status", "--porcelain", "--untracked-files=no"],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=8.0,
                check=False,
            )
            if status_proc.returncode != 0 or status_proc.stdout.strip():
                return None

            fetch_proc = subprocess.run(
                ["git", "--no-pager", "fetch", "origin", "main", "--tags", "--quiet"],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20.0,
                check=False,
            )
            if fetch_proc.returncode != 0:
                return None

            ff_proc = subprocess.run(
                ["git", "--no-pager", "merge", "--ff-only", "origin/main"],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15.0,
                check=False,
            )
            if ff_proc.returncode != 0:
                return None
            export_extension_bundle(self.root_dir)
            return {
                "ok": True,
                "strategy": "git_ff",
                "target_tag": target_tag,
            }
        except Exception:
            return None

    def update_backend_and_extension(
        self,
        manifest_override: Optional[Dict[str, Any]] = None,
        allow_git: bool = True,
    ) -> Dict[str, Any]:
        """Update backend + extension from GitHub Release while preserving `.env` & `usage_stats.json`."""
        info = manifest_override or self.check_github_update(force=True)
        if not info.get("ok") and not manifest_override:
            raise UpdateError(info.get("error") or "无法获取 GitHub Release 元数据")

        target_ver = normalize_version(
            str(info.get("latest_github_version") or info.get("version") or "")
        )
        tag = str(info.get("tag") or (f"v{target_ver}" if target_ver else ""))

        # Backup protected user files in memory as an extra safety net
        protected_backups: Dict[Path, bytes] = {}
        for fname in PROTECTED_USER_FILES:
            fpath = self.root_dir / fname
            if fpath.is_file():
                protected_backups[fpath] = fpath.read_bytes()

        try:
            if allow_git and self._url_fetcher is _default_url_fetcher:
                git_res = self._try_git_fast_forward(target_tag=tag)
                if git_res is not None:
                    git_res["updated_version"] = target_ver
                    return git_res

            dl_urls = info.get("download_urls") or {}
            checksums = info.get("checksums") or {}
            backend_url = str(
                info.get("backend_zip_url") or dl_urls.get("backend_zip") or ""
            ).strip()
            backend_sha = str(
                info.get("backend_zip_sha256") or checksums.get("backend_zip_sha256") or ""
            ).strip()
            if not backend_url:
                raise UpdateError("缺少 backend_zip_url 下载地址")

            with tempfile.TemporaryDirectory(prefix="geb_backend_upd_") as tmp:
                tmp_dir = Path(tmp)
                zip_file = tmp_dir / "gemini-enterprise-bridge-backend.zip"
                verified_sha = self._download_verified_asset(
                    backend_url,
                    "gemini-enterprise-bridge-backend.zip",
                    backend_sha,
                    zip_file,
                    tag=tag,
                    timeout=60.0,
                )
                stage_dir = tmp_dir / "stage"
                stage_dir.mkdir(parents=True, exist_ok=True)
                extracted = _safe_extract_zip(
                    zip_file, stage_dir, preserve_relative_paths=set(PROTECTED_USER_FILES)
                )

                for src_file in stage_dir.rglob("*"):
                    if not src_file.is_file():
                        continue
                    rel = src_file.relative_to(stage_dir)
                    if rel.as_posix() in PROTECTED_USER_FILES or rel.name in PROTECTED_USER_FILES:
                        if (self.root_dir / rel).exists():
                            continue
                    dst = self.root_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst)

            # Re-materialize unpacked `extension/` and `userscript/` on disk
            try:
                export_extension_bundle(self.root_dir)
            except Exception:
                pass

            return {
                "ok": True,
                "strategy": "release_zip",
                "updated_version": target_ver,
                "sha256": verified_sha,
                "files_updated": len(extracted),
            }
        finally:
            # Guarantee `.env` and `usage_stats.json` remain intact byte-for-byte
            for fpath, raw_bytes in protected_backups.items():
                if not fpath.is_file() or fpath.read_bytes() != raw_bytes:
                    fpath.write_bytes(raw_bytes)


def run_startup_auto_update(root_dir: Optional[Path] = None, check_only: bool = False) -> int:
    """CLI entrypoint invoked by `start.ps1` before launching `server.py`."""
    auto_flag = os.getenv("BRIDGE_AUTO_UPDATE", "true").strip().lower()
    if auto_flag in {"0", "false", "no", "off"}:
        print("[Updater] BRIDGE_AUTO_UPDATE=false，跳过启动自动更新检查。")
        return 0

    mgr = UpdateManager(root_dir=root_dir)
    status = mgr.check_github_update(force=True, timeout=6.0)
    if not status.get("ok"):
        print(f"[Updater] 跳过远程更新检查（离线或尚无 Release: {status.get('error')}）")
        return 0

    remote_ver = status.get("latest_github_version") or ""
    if compare_semver(remote_ver, mgr.current_version) <= 0:
        print(f"[Updater] 当前已是最新版本 v{mgr.current_version}。")
        return 0

    print(f"[Updater] 发现 GitHub 新版本 v{remote_ver}（当前本地 v{mgr.current_version}）！")
    if check_only:
        return 0

    try:
        res = mgr.update_backend_and_extension(manifest_override=status, allow_git=True)
        print(
            f"[Updater] 已自动更新至 v{res.get('updated_version') or remote_ver} "
            f"(方式: {res.get('strategy')})，extension/ 与后端文件已同步！"
        )
    except Exception as exc:
        print(f"[Updater] 自动更新未完成（继续启动现有版本 v{mgr.current_version}）: {exc}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Enterprise Bridge GitHub Release Auto-Updater")
    parser.add_argument(
        "--startup-update",
        action="store_true",
        help="Check GitHub Releases on startup and automatically apply newer release if available",
    )
    parser.add_argument(
        "--startup-check",
        action="store_true",
        help="Only check GitHub Releases and print version status without modifying files",
    )
    parser.add_argument(
        "--sync-client-limits",
        action="store_true",
        help="Sync 1,048,576 prompt / 65,536 output token limits into local Copilot App BYOK provider_models",
    )
    args = parser.parse_args()
    if args.sync_client_limits:
        from sync_client_limits import sync_copilot_provider_limits

        res = sync_copilot_provider_limits()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0 if res.get("ok") else 1)
    root = Path(__file__).resolve().parent
    if args.startup_check:
        sys.exit(run_startup_auto_update(root_dir=root, check_only=True))
    sys.exit(run_startup_auto_update(root_dir=root, check_only=False))


if __name__ == "__main__":
    main()

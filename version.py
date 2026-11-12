"""Single source of truth for Gemini Enterprise Bridge versioning and GitHub Release endpoints."""

from __future__ import annotations

import os

APP_VERSION = "1.1.0"
DEFAULT_GITHUB_REPO = "StevenLee1471717/Gemini-Enterprise-Bridge"


def get_github_repo() -> str:
    """Return configured GitHub owner/repo (overridable via BRIDGE_GITHUB_REPO)."""
    raw = os.getenv("BRIDGE_GITHUB_REPO", DEFAULT_GITHUB_REPO).strip().strip("/")
    return raw or DEFAULT_GITHUB_REPO


def get_releases_latest_api(repo: str | None = None) -> str:
    target = (repo or get_github_repo()).strip().strip("/")
    return f"https://api.github.com/repos/{target}/releases/latest"


def get_release_download_base(repo: str | None = None) -> str:
    target = (repo or get_github_repo()).strip().strip("/")
    return f"https://github.com/{target}/releases/latest/download"


GITHUB_REPO = get_github_repo()
GITHUB_RELEASES_LATEST_API = get_releases_latest_api(GITHUB_REPO)
GITHUB_RELEASE_DOWNLOAD_BASE = get_release_download_base(GITHUB_REPO)

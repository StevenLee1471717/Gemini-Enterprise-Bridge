"""Configuration management for Gemini Enterprise Bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from version import DEFAULT_GITHUB_REPO


def _load_dotenv_file(dotenv_path: Path) -> None:
    """Load key=value pairs from .env without failing if python-dotenv is absent."""
    if not dotenv_path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=dotenv_path, override=False)
        return
    except ImportError:
        pass

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class BridgeSettings:
    """Runtime settings loaded from environment variables or .env."""

    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = ""
    backend_mode: str = "browser"  # browser | auto | gcloud
    context_mode: str = "smart"  # smart | last_message | transcript
    default_model: str = "gemini-enterprise-web"
    request_timeout_seconds: float = 600.0
    queue_timeout_seconds: float = 300.0
    auto_reset_new_chat: bool = True
    max_parallel_tabs: int = 6
    auto_spawn_tabs: bool = True
    session_affinity_ttl_seconds: float = 300.0
    include_citations: bool = True
    cors_allow_origins: List[str] = field(default_factory=lambda: ["*"])
    auto_export_extension: bool = True
    auto_update: bool = True
    github_repo: str = DEFAULT_GITHUB_REPO
    github_token: str = ""
    usage_stats_path: str = ""
    usd_to_cny_rate: float = 6.8
    generated_dir: str = ""
    response_store_dir: str = ""
    response_store_ttl_seconds: float = 86400.0

    # Optional Option-1 (Official GCloud CLI / Vertex AI / Discovery Engine) settings
    gcp_project_id: str = ""
    gcp_location: str = "global"
    gcp_vertex_location: str = "us-central1"
    gcp_vertex_model: str = "gemini-2.5-pro"
    gcp_engine_id: str = ""
    gcp_collection_id: str = "default_collection"
    gcloud_binary: str = "gcloud"

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "BridgeSettings":
        root = base_dir or Path(__file__).resolve().parent
        _load_dotenv_file(root / ".env")

        origins_raw = os.getenv("BRIDGE_CORS_ORIGINS", "*").strip()
        origins = [item.strip() for item in origins_raw.split(",") if item.strip()] or ["*"]

        backend_mode = os.getenv("BRIDGE_BACKEND_MODE", "browser").strip().lower()
        if backend_mode not in {"browser", "auto", "gcloud"}:
            backend_mode = "browser"

        context_mode = os.getenv("BRIDGE_CONTEXT_MODE", "smart").strip().lower()
        if context_mode not in {"smart", "last_message", "transcript"}:
            context_mode = "smart"

        return cls(
            host=os.getenv("BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("BRIDGE_PORT", 8000),
            api_key=os.getenv("BRIDGE_API_KEY", "").strip(),
            backend_mode=backend_mode,
            context_mode=context_mode,
            default_model=os.getenv("BRIDGE_DEFAULT_MODEL", "gemini-enterprise-web").strip()
            or "gemini-enterprise-web",
            request_timeout_seconds=_env_float("BRIDGE_REQUEST_TIMEOUT", 600.0),
            queue_timeout_seconds=_env_float("BRIDGE_QUEUE_TIMEOUT", 300.0),
            auto_reset_new_chat=_env_bool("BRIDGE_AUTO_RESET_NEW_CHAT", True),
            max_parallel_tabs=max(1, _env_int("BRIDGE_MAX_PARALLEL_TABS", 6)),
            auto_spawn_tabs=_env_bool("BRIDGE_AUTO_SPAWN_TABS", True),
            session_affinity_ttl_seconds=max(
                0.0, _env_float("BRIDGE_SESSION_AFFINITY_TTL", 300.0)
            ),
            include_citations=_env_bool("BRIDGE_INCLUDE_CITATIONS", True),
            cors_allow_origins=origins,
            auto_export_extension=_env_bool("BRIDGE_AUTO_EXPORT_EXTENSION", True),
            auto_update=_env_bool("BRIDGE_AUTO_UPDATE", True),
            github_repo=os.getenv("BRIDGE_GITHUB_REPO", DEFAULT_GITHUB_REPO).strip().strip("/")
            or DEFAULT_GITHUB_REPO,
            github_token=(
                os.getenv("BRIDGE_GITHUB_TOKEN", "").strip()
                or os.getenv("GITHUB_TOKEN", "").strip()
            ),
            usage_stats_path=os.getenv("BRIDGE_USAGE_STATS_PATH", "").strip(),
            usd_to_cny_rate=_env_float("BRIDGE_USD_TO_CNY_RATE", 6.8),
            generated_dir=os.getenv("BRIDGE_GENERATED_DIR", "").strip(),
            response_store_dir=os.getenv(
                "BRIDGE_RESPONSE_STORE_DIR", str(root / ".cache" / "response_store")
            ).strip(),
            response_store_ttl_seconds=max(
                60.0, _env_float("BRIDGE_RESPONSE_STORE_TTL", 86400.0)
            ),
            gcp_project_id=os.getenv("GCP_PROJECT_ID", "").strip(),
            gcp_location=os.getenv("GCP_LOCATION", "global").strip() or "global",
            gcp_vertex_location=os.getenv("GCP_VERTEX_LOCATION", "us-central1").strip()
            or "us-central1",
            gcp_vertex_model=os.getenv("GCP_VERTEX_MODEL", "gemini-2.5-pro").strip()
            or "gemini-2.5-pro",
            gcp_engine_id=os.getenv("GCP_ENGINE_ID", "").strip(),
            gcp_collection_id=os.getenv("GCP_COLLECTION_ID", "default_collection").strip()
            or "default_collection",
            gcloud_binary=os.getenv("GCLOUD_BINARY", "gcloud").strip() or "gcloud",
        )

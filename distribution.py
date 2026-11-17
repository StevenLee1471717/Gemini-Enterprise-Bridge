"""Public API distribution, multi-client authentication, tenant isolation, and host-first traffic scheduling."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from schemas import parse_model_and_session, resolve_target_web_model

logger = logging.getLogger("gemini_bridge.distribution")

HOST_TENANT_ID = "host"
try:
    DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    DEFAULT_TZ = timezone(timedelta(hours=8))

_FORWARDING_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "cf-connecting-ip",
    "true-client-ip",
    "x-client-ip",
    "cf-ray",
)

_LOCAL_PEER_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_LOCAL_HOST_HEADERS = {"127.0.0.1", "::1", "[::1]", "localhost", "0.0.0.0"}


class DistributionAuthError(PermissionError):
    """Raised when API authentication or access boundary checks fail."""

    def __init__(self, message: str, status_code: int = 401, code: str = "invalid_api_key") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class DistributionQuotaError(RuntimeError):
    """Raised when a client exceeds concurrency queue timeout, RPM rate limit, or daily quota."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        code: str = "rate_limit_exceeded",
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


def _normalize_peer_ip(raw_host: Optional[str]) -> str:
    val = (raw_host or "").strip().lower()
    if val.startswith("::ffff:"):
        val = val[7:]
    return val


def _extract_host_only(raw_host_header: Optional[str]) -> str:
    raw = (raw_host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    if raw.count(":") == 1:
        return raw.split(":", 1)[0].strip()
    return raw


def is_direct_local_connection(conn: Any) -> bool:
    """Return True ONLY for genuine local loopback requests with no reverse-proxy/tunnel forwarding."""
    client_obj = getattr(conn, "client", None)
    peer_ip = _normalize_peer_ip(getattr(client_obj, "host", None) if client_obj else None)
    if peer_ip not in _LOCAL_PEER_HOSTS:
        return False

    headers = getattr(conn, "headers", None)
    if headers is not None:
        for h_name in _FORWARDING_HEADERS:
            if (headers.get(h_name) or "").strip():
                return False
        host_only = _extract_host_only(headers.get("host"))
        allowed_hosts = _LOCAL_HOST_HEADERS | ({"testserver", "testclient"} if peer_ip == "testclient" else set())
        if host_only and host_only not in allowed_hosts and not host_only.endswith(".local"):
            return False
    return True


def extract_client_ip(conn: Any) -> str:
    """Best-effort real client IP extraction across tunnels, reverse proxies, and direct sockets."""
    headers = getattr(conn, "headers", None)
    if headers is not None:
        for key in ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-client-ip"):
            val = (headers.get(key) or "").strip()
            if val:
                return val.split(",", 1)[0].strip()
        xff = (headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",", 1)[0].strip()
    client_obj = getattr(conn, "client", None)
    return _normalize_peer_ip(getattr(client_obj, "host", None) if client_obj else None) or "unknown"


def resolve_public_base_url(
    conn: Any,
    configured_public_base_url: str = "",
    fallback_origin: str = "http://127.0.0.1:8000",
) -> str:
    """Build an externally reachable origin URL for generated file/image links."""
    configured = (configured_public_base_url or "").strip().rstrip("/")
    if is_direct_local_connection(conn):
        return fallback_origin.rstrip("/")
    if configured:
        return configured
    headers = getattr(conn, "headers", None)
    if headers is not None:
        fwd_host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",", 1)[0].strip()
        if fwd_host:
            fwd_proto = (headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
            if not fwd_proto:
                if headers.get("cf-connecting-ip") or headers.get("cf-ray"):
                    fwd_proto = "https"
                else:
                    url_obj = getattr(conn, "url", None)
                    fwd_proto = getattr(url_obj, "scheme", "") or "http"
            return f"{fwd_proto}://{fwd_host}".rstrip("/")
    return fallback_origin.rstrip("/")


def extract_bearer_or_api_key(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _mask_key(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 14:
        return secret[:4] + "****" + secret[-2:]
    return f"{secret[:10]}...{secret[-4:]}"


def _today_shanghai() -> str:
    return datetime.now(DEFAULT_TZ).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class RequestPrincipal:
    """Authenticated caller identity resolved per incoming API request."""

    tenant_id: str = HOST_TENANT_ID
    client_id: Optional[str] = None
    name: str = "本机直连"
    is_host: bool = True
    is_direct_local: bool = True
    client_ip: str = "127.0.0.1"

    def scope_session_id(self, explicit_session_id: Optional[str]) -> Optional[str]:
        if not explicit_session_id or not explicit_session_id.strip():
            return None
        cleaned = explicit_session_id.strip()
        if self.is_host:
            return cleaned
        prefix = f"{self.tenant_id}::"
        return cleaned if cleaned.startswith(prefix) else f"{prefix}{cleaned}"


@dataclass
class ClientRecord:
    client_id: str
    name: str
    api_key: str
    key_hash: str
    note: str = ""
    enabled: bool = True
    max_concurrency: int = 2
    rpm_limit: int = 30
    daily_request_limit: int = 0
    daily_token_limit: int = 0
    allowed_models: List[str] = field(default_factory=list)
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_used_at: int = 0
    last_ip: str = ""
    stats_day: str = field(default_factory=_today_shanghai)
    today_requests: int = 0
    today_tokens: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    total_errors: int = 0

    def refresh_day(self, today: Optional[str] = None) -> None:
        cur = today or _today_shanghai()
        if self.stats_day != cur:
            self.stats_day = cur
            self.today_requests = 0
            self.today_tokens = 0

    def to_storage_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "api_key": self.api_key,
            "key_hash": self.key_hash,
            "note": self.note,
            "enabled": self.enabled,
            "max_concurrency": self.max_concurrency,
            "rpm_limit": self.rpm_limit,
            "daily_request_limit": self.daily_request_limit,
            "daily_token_limit": self.daily_token_limit,
            "allowed_models": list(self.allowed_models),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_ip": self.last_ip,
            "stats_day": self.stats_day,
            "today_requests": self.today_requests,
            "today_tokens": self.today_tokens,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_errors": self.total_errors,
        }

    def to_view_dict(self, *, include_secret: bool = False, in_flight: int = 0, queued: int = 0) -> Dict[str, Any]:
        self.refresh_day()
        out: Dict[str, Any] = {
            "client_id": self.client_id,
            "tenant_id": f"client:{self.client_id}",
            "name": self.name,
            "note": self.note,
            "key_preview": _mask_key(self.api_key),
            "enabled": self.enabled,
            "max_concurrency": self.max_concurrency,
            "rpm_limit": self.rpm_limit,
            "daily_request_limit": self.daily_request_limit,
            "daily_token_limit": self.daily_token_limit,
            "allowed_models": list(self.allowed_models),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_ip": self.last_ip,
            "today_requests": self.today_requests,
            "today_tokens": self.today_tokens,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_errors": self.total_errors,
            "in_flight": in_flight,
            "queued": queued,
        }
        if include_secret:
            out["api_key"] = self.api_key
        return out


@dataclass
class _WaitTicket:
    seq: int
    client_id: str


class DistributionManager:
    """Manages distributed client API keys, host-reserved slots, per-client concurrency, and fair queueing."""

    def __init__(
        self,
        storage_path: Optional[str | Path] = None,
        default_host_reserved_slots: int = 2,
    ) -> None:
        self._storage_path = Path(storage_path) if storage_path else None
        self._lock = threading.RLock()

        self.external_access_enabled: bool = True
        self.host_reserved_slots: int = max(0, int(default_host_reserved_slots))
        self.external_queue_timeout_seconds: float = 90.0
        self.public_base_url: str = ""

        self._clients: Dict[str, ClientRecord] = {}
        self._rpm_windows: Dict[str, Deque[float]] = {}

        # Live runtime concurrency counters
        self._host_in_flight: int = 0
        self._host_waiting: int = 0
        self._external_in_flight: int = 0
        self._client_in_flight: Dict[str, int] = {}
        self._client_queued: Dict[str, int] = {}
        self._wait_tickets: List[_WaitTicket] = []
        self._ticket_seq: int = 0
        self._loop_conditions: Dict[int, asyncio.Condition] = {}

        if self._storage_path is not None:
            self._load()

    def _get_condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        lid = id(loop)
        cond = self._loop_conditions.get(lid)
        if cond is None:
            if len(self._loop_conditions) > 16:
                self._loop_conditions.clear()
            cond = asyncio.Condition()
            self._loop_conditions[lid] = cond
        return cond

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._storage_path is None or not self._storage_path.is_file():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("加载分发配置文件失败 (%s): %s", self._storage_path, exc)
            return
        if not isinstance(raw, dict):
            return
        policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else raw
        if "external_access_enabled" in policy:
            self.external_access_enabled = bool(policy["external_access_enabled"])
        if "host_reserved_slots" in policy:
            try:
                self.host_reserved_slots = max(0, min(32, int(policy["host_reserved_slots"])))
            except (TypeError, ValueError):
                pass
        if "external_queue_timeout_seconds" in policy:
            try:
                self.external_queue_timeout_seconds = max(
                    5.0, min(600.0, float(policy["external_queue_timeout_seconds"]))
                )
            except (TypeError, ValueError):
                pass
        if isinstance(policy.get("public_base_url"), str):
            self.public_base_url = policy["public_base_url"].strip().rstrip("/")

        today = _today_shanghai()
        loaded_clients: Dict[str, ClientRecord] = {}
        for item in raw.get("clients") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("client_id") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            if not cid or not api_key:
                continue
            key_hash = str(item.get("key_hash") or _hash_secret(api_key))
            rec = ClientRecord(
                client_id=cid,
                name=str(item.get("name") or cid).strip()[:64],
                api_key=api_key,
                key_hash=key_hash,
                note=str(item.get("note") or "").strip()[:200],
                enabled=bool(item.get("enabled", True)),
                max_concurrency=max(1, min(20, int(item.get("max_concurrency") or 2))),
                rpm_limit=max(0, min(1200, int(item.get("rpm_limit") if item.get("rpm_limit") is not None else 30))),
                daily_request_limit=max(0, int(item.get("daily_request_limit") or 0)),
                daily_token_limit=max(0, int(item.get("daily_token_limit") or 0)),
                allowed_models=[
                    str(m).strip() for m in (item.get("allowed_models") or []) if str(m).strip()
                ],
                created_at=int(item.get("created_at") or time.time()),
                last_used_at=int(item.get("last_used_at") or 0),
                last_ip=str(item.get("last_ip") or ""),
                stats_day=str(item.get("stats_day") or today),
                today_requests=max(0, int(item.get("today_requests") or 0)),
                today_tokens=max(0, int(item.get("today_tokens") or 0)),
                total_requests=max(0, int(item.get("total_requests") or 0)),
                total_tokens=max(0, int(item.get("total_tokens") or 0)),
                total_errors=max(0, int(item.get("total_errors") or 0)),
            )
            rec.refresh_day(today)
            loaded_clients[cid] = rec
        self._clients = loaded_clients

    def _save_locked(self) -> None:
        if self._storage_path is None:
            return
        payload = {
            "version": 1,
            "updated_at": int(time.time()),
            "policy": {
                "external_access_enabled": self.external_access_enabled,
                "host_reserved_slots": self.host_reserved_slots,
                "external_queue_timeout_seconds": self.external_queue_timeout_seconds,
                "public_base_url": self.public_base_url,
            },
            "clients": [c.to_storage_dict() for c in self._clients.values()],
        }
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".dist_", suffix=".json", dir=str(self._storage_path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self._storage_path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except Exception as exc:
            logger.warning("保存分发配置文件失败 (%s): %s", self._storage_path, exc)

    # ------------------------------------------------------------------
    # Policy & Client CRUD (Host Admin)
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_client_api_key() -> str:
        return "sk-geb-" + secrets.token_urlsafe(24)

    def update_policy(
        self,
        *,
        external_access_enabled: Optional[bool] = None,
        host_reserved_slots: Optional[int] = None,
        external_queue_timeout_seconds: Optional[float] = None,
        public_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if external_access_enabled is not None:
                self.external_access_enabled = bool(external_access_enabled)
            if host_reserved_slots is not None:
                self.host_reserved_slots = max(0, min(32, int(host_reserved_slots)))
            if external_queue_timeout_seconds is not None:
                self.external_queue_timeout_seconds = max(
                    5.0, min(600.0, float(external_queue_timeout_seconds))
                )
            if public_base_url is not None:
                cleaned_url = public_base_url.strip().rstrip("/")
                if cleaned_url.endswith("/v1"):
                    cleaned_url = cleaned_url[:-3].rstrip("/")
                self.public_base_url = cleaned_url
            self._save_locked()
            return self.snapshot(include_secrets=True)

    def create_client(
        self,
        *,
        name: str,
        note: str = "",
        max_concurrency: int = 2,
        rpm_limit: int = 30,
        daily_request_limit: int = 0,
        daily_token_limit: int = 0,
        allowed_models: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()[:64]
        if not clean_name:
            raise ValueError("客户端名称不能为空。")
        with self._lock:
            cid = "cli_" + secrets.token_hex(6)
            while cid in self._clients:
                cid = "cli_" + secrets.token_hex(6)
            raw_key = self._generate_client_api_key()
            rec = ClientRecord(
                client_id=cid,
                name=clean_name,
                api_key=raw_key,
                key_hash=_hash_secret(raw_key),
                note=(note or "").strip()[:200],
                enabled=True,
                max_concurrency=max(1, min(20, int(max_concurrency or 2))),
                rpm_limit=max(0, min(1200, int(rpm_limit if rpm_limit is not None else 30))),
                daily_request_limit=max(0, int(daily_request_limit or 0)),
                daily_token_limit=max(0, int(daily_token_limit or 0)),
                allowed_models=[str(m).strip() for m in (allowed_models or []) if str(m).strip()],
            )
            self._clients[cid] = rec
            self._save_locked()
            return rec.to_view_dict(include_secret=True)

    def update_client(
        self,
        client_id: str,
        *,
        name: Optional[str] = None,
        note: Optional[str] = None,
        enabled: Optional[bool] = None,
        max_concurrency: Optional[int] = None,
        rpm_limit: Optional[int] = None,
        daily_request_limit: Optional[int] = None,
        daily_token_limit: Optional[int] = None,
        allowed_models: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec is None:
                raise KeyError(f"未找到客户端：{client_id}")
            if name is not None:
                cleaned = name.strip()[:64]
                if not cleaned:
                    raise ValueError("客户端名称不能为空。")
                rec.name = cleaned
            if note is not None:
                rec.note = note.strip()[:200]
            if enabled is not None:
                rec.enabled = bool(enabled)
            if max_concurrency is not None:
                rec.max_concurrency = max(1, min(20, int(max_concurrency)))
            if rpm_limit is not None:
                rec.rpm_limit = max(0, min(1200, int(rpm_limit)))
            if daily_request_limit is not None:
                rec.daily_request_limit = max(0, int(daily_request_limit))
            if daily_token_limit is not None:
                rec.daily_token_limit = max(0, int(daily_token_limit))
            if allowed_models is not None:
                rec.allowed_models = [str(m).strip() for m in allowed_models if str(m).strip()]
            self._save_locked()
            return rec.to_view_dict(
                include_secret=True,
                in_flight=self._client_in_flight.get(client_id, 0),
                queued=self._client_queued.get(client_id, 0),
            )

    def rotate_client_key(self, client_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec is None:
                raise KeyError(f"未找到客户端：{client_id}")
            new_key = self._generate_client_api_key()
            rec.api_key = new_key
            rec.key_hash = _hash_secret(new_key)
            self._save_locked()
            return rec.to_view_dict(
                include_secret=True,
                in_flight=self._client_in_flight.get(client_id, 0),
                queued=self._client_queued.get(client_id, 0),
            )

    def delete_client(self, client_id: str) -> bool:
        with self._lock:
            removed = self._clients.pop(client_id, None)
            self._rpm_windows.pop(client_id, None)
            if removed is None:
                return False
            self._save_locked()
            return True

    def get_tenant_label(self, tenant_id: Optional[str]) -> str:
        if not tenant_id or tenant_id == HOST_TENANT_ID:
            return "本机直连"
        if tenant_id.startswith("client:"):
            cid = tenant_id.split(":", 1)[1]
            with self._lock:
                rec = self._clients.get(cid)
                if rec is not None:
                    return f"客户端 · {rec.name}"
            return f"已吊销客户端 ({cid[:8]})"
        return tenant_id

    # ------------------------------------------------------------------
    # Authentication & Boundary Resolution
    # ------------------------------------------------------------------

    def _match_client_by_token_locked(self, token: str) -> Optional[ClientRecord]:
        if not token:
            return None
        digest = _hash_secret(token)
        matched: Optional[ClientRecord] = None
        for rec in self._clients.values():
            if hmac.compare_digest(rec.key_hash, digest):
                matched = rec
        return matched

    def authenticate_request(
        self,
        conn: Any,
        *,
        master_api_key: str = "",
        authorization: Optional[str] = None,
        x_api_key: Optional[str] = None,
    ) -> RequestPrincipal:
        """Authenticate an incoming OpenAI-compatible API call and return its isolated tenant principal."""
        direct_local = is_direct_local_connection(conn)
        client_ip = extract_client_ip(conn)
        headers = getattr(conn, "headers", None)
        eff_auth = authorization if authorization is not None else (headers.get("authorization") if headers else None)
        eff_xkey = x_api_key if x_api_key is not None else (headers.get("x-api-key") if headers else None)
        token = extract_bearer_or_api_key(eff_auth, eff_xkey)

        # 1) Explicit issued Client Key (`sk-geb-...`) always resolves to that client tenant
        #    (even when tested locally from 127.0.0.1).
        if token:
            with self._lock:
                matched = self._match_client_by_token_locked(token)
                if matched is not None:
                    if not self.external_access_enabled:
                        raise DistributionAuthError(
                            "主机管理员已临时暂停外部客户端接入。",
                            status_code=403,
                            code="external_access_paused",
                        )
                    if not matched.enabled:
                        raise DistributionAuthError(
                            f"客户端密钥（{matched.name}）已被暂停使用，请联系主机管理员。",
                            status_code=403,
                            code="client_disabled",
                        )
                    return RequestPrincipal(
                        tenant_id=f"client:{matched.client_id}",
                        client_id=matched.client_id,
                        name=matched.name,
                        is_host=False,
                        is_direct_local=direct_local,
                        client_ip=client_ip,
                    )
            # If caller explicitly sent a `sk-geb-` key that doesn't exist, reject immediately
            # instead of falling through to keyless localhost.
            if token.startswith("sk-geb-"):
                raise DistributionAuthError(
                    "客户端专属 API Key 无效或已被吊销。",
                    status_code=401,
                    code="invalid_api_key",
                )

        # 2) Direct localhost traffic: honor existing host `BRIDGE_API_KEY` (empty = keyless local access)
        if direct_local:
            if master_api_key:
                if not token or not hmac.compare_digest(token, master_api_key):
                    raise DistributionAuthError(
                        "API Key 校验未通过，请检查客户端 Authorization Bearer 配置。",
                        status_code=401,
                        code="invalid_api_key",
                    )
            return RequestPrincipal(
                tenant_id=HOST_TENANT_ID,
                client_id=None,
                name="本机直连",
                is_host=True,
                is_direct_local=True,
                client_ip=client_ip,
            )

        # 3) External / reverse-proxy / tunnel / LAN traffic:
        #    NEVER allow keyless public access even when local `BRIDGE_API_KEY` is blank.
        if not self.external_access_enabled:
            raise DistributionAuthError(
                "主机管理员已关闭公网外部接入。",
                status_code=403,
                code="external_access_paused",
            )
        if master_api_key and token and hmac.compare_digest(token, master_api_key):
            return RequestPrincipal(
                tenant_id=HOST_TENANT_ID,
                client_id=None,
                name="主机管理员密钥",
                is_host=True,
                is_direct_local=False,
                client_ip=client_ip,
            )
        raise DistributionAuthError(
            "公网/远程接入需要有效的客户端专属 API Key（请在本机控制台『公网分发与多客分流』中生成）。",
            status_code=401,
            code="invalid_api_key",
        )

    # ------------------------------------------------------------------
    # Host-First Fair Concurrency, Rate Limit & Quota Gate
    # ------------------------------------------------------------------

    def _compute_external_cap(self, pool_capacity: int) -> int:
        if pool_capacity <= 0:
            return 64  # No connected browser tabs; let downstream fail fast with 503
        if pool_capacity > self.host_reserved_slots:
            return max(1, pool_capacity - self.host_reserved_slots)
        # Pool size <= reserved slots (e.g. 1-tab fixed mode): allow 1 external request ONLY while Host is idle
        if self._host_in_flight == 0 and self._host_waiting == 0:
            return 1
        return 0

    def _check_model_allowed_locked(self, rec: ClientRecord, requested_model: Optional[str]) -> None:
        if not rec.allowed_models:
            return
        raw = (requested_model or "").strip()
        base_model, _ = parse_model_and_session(raw)
        target_web = resolve_target_web_model(base_model)
        allowed_norm = {m.strip().lower() for m in rec.allowed_models if m.strip()}
        allowed_web = {resolve_target_web_model(m) for m in rec.allowed_models if m.strip()}
        if base_model.lower() in allowed_norm or (target_web and target_web in allowed_web):
            return
        raise DistributionAuthError(
            f"当前客户端密钥仅允许调用模型：{', '.join(rec.allowed_models)}。",
            status_code=403,
            code="model_not_allowed",
        )

    def filter_models_for_principal(
        self, principal: RequestPrincipal, models: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if principal.is_host or not principal.client_id:
            return list(models)
        with self._lock:
            rec = self._clients.get(principal.client_id)
            if rec is None or not rec.allowed_models:
                return list(models)
            allowed_norm = {m.strip().lower() for m in rec.allowed_models if m.strip()}
            allowed_web = {
                w for w in (resolve_target_web_model(m) for m in rec.allowed_models if m.strip()) if w
            }
            filtered: List[Dict[str, Any]] = []
            for item in models:
                mid = str(item.get("id") or "").strip()
                m_web = resolve_target_web_model(mid)
                if mid.lower() in allowed_norm or (m_web and m_web in allowed_web):
                    filtered.append(item)
            return filtered or list(models)

    def _check_daily_quota_locked(self, rec: ClientRecord) -> None:
        rec.refresh_day()
        if rec.daily_request_limit > 0 and rec.today_requests >= rec.daily_request_limit:
            raise DistributionQuotaError(
                f"已达到今日请求次数上限（{rec.daily_request_limit} 次/日），请明日再试或联系管理员调整配额。",
                status_code=429,
                code="daily_quota_exceeded",
            )
        if rec.daily_token_limit > 0 and rec.today_tokens >= rec.daily_token_limit:
            raise DistributionQuotaError(
                f"已达到今日 Token 用量上限（{rec.daily_token_limit:,} Tokens/日），请明日再试或联系管理员调整配额。",
                status_code=429,
                code="daily_quota_exceeded",
            )

    def _consume_rpm_slot_locked(self, rec: ClientRecord, now_mono: float) -> None:
        if rec.rpm_limit <= 0:
            return
        win = self._rpm_windows.setdefault(rec.client_id, deque())
        cutoff = now_mono - 60.0
        while win and win[0] <= cutoff:
            win.popleft()
        if len(win) >= rec.rpm_limit:
            retry_after = max(1, int(math.ceil(60.0 - (now_mono - win[0]))))
            raise DistributionQuotaError(
                f"请求过于频繁：已达每分钟 {rec.rpm_limit} 次上限，请约 {retry_after} 秒后重试。",
                status_code=429,
                code="rate_limit_exceeded",
                retry_after=retry_after,
            )
        win.append(now_mono)

    def _is_fair_winner_locked(self, ticket: _WaitTicket) -> bool:
        best_rank: Optional[tuple[int, int]] = None
        best_seq: Optional[int] = None
        for candidate in self._wait_tickets:
            crec = self._clients.get(candidate.client_id)
            if crec is None or not crec.enabled:
                continue
            cur_inf = self._client_in_flight.get(candidate.client_id, 0)
            if cur_inf >= crec.max_concurrency:
                continue
            rank = (cur_inf, candidate.seq)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_seq = candidate.seq
        return best_seq == ticket.seq

    def preflight_check(
        self,
        principal: RequestPrincipal,
        *,
        requested_model: Optional[str] = None,
    ) -> None:
        """Fast synchronous check for model allowlist and daily quota before upstream dispatch."""
        if principal.is_host or not principal.client_id:
            return
        with self._lock:
            rec = self._clients.get(principal.client_id)
            if rec is None:
                raise DistributionAuthError("客户端密钥已被吊销。", status_code=401, code="invalid_api_key")
            if not self.external_access_enabled:
                raise DistributionAuthError("主机管理员已暂停公网外部接入。", status_code=403, code="external_access_paused")
            if not rec.enabled:
                raise DistributionAuthError("客户端密钥已被暂停使用。", status_code=403, code="client_disabled")
            self._check_model_allowed_locked(rec, requested_model)
            self._check_daily_quota_locked(rec)

    @contextlib.asynccontextmanager
    async def acquire_execution_permit(
        self,
        principal: RequestPrincipal,
        *,
        requested_model: Optional[str] = None,
        capacity_provider: Optional[Callable[[], int]] = None,
        queue_timeout: Optional[float] = None,
    ) -> AsyncIterator[None]:
        """Acquire a host-prioritized or fair client-throttled execution lease."""
        cond = self._get_condition()

        if principal.is_host or not principal.client_id:
            async with cond:
                self._host_in_flight += 1
                cond.notify_all()
            try:
                yield
            finally:
                async with cond:
                    self._host_in_flight = max(0, self._host_in_flight - 1)
                    cond.notify_all()
            return

        cid = principal.client_id
        with self._lock:
            rec = self._clients.get(cid)
            if rec is None:
                raise DistributionAuthError("客户端密钥已被吊销。", status_code=401, code="invalid_api_key")
            if not self.external_access_enabled:
                raise DistributionAuthError("主机管理员已暂停公网外部接入。", status_code=403, code="external_access_paused")
            if not rec.enabled:
                raise DistributionAuthError("客户端密钥已被暂停使用。", status_code=403, code="client_disabled")
            self._check_model_allowed_locked(rec, requested_model)
            self._check_daily_quota_locked(rec)
            max_waiters = max(6, rec.max_concurrency * 4)
            if self._client_queued.get(cid, 0) >= max_waiters:
                raise DistributionQuotaError(
                    f"当前客户端并发排队已满（上限 {rec.max_concurrency} 并发），请稍后重试。",
                    status_code=429,
                    code="client_queue_full",
                )
            eff_timeout = min(
                float(queue_timeout) if queue_timeout is not None else self.external_queue_timeout_seconds,
                self.external_queue_timeout_seconds,
            )

        deadline = time.monotonic() + max(1.0, eff_timeout)
        admitted = False
        async with cond:
            self._ticket_seq += 1
            ticket = _WaitTicket(seq=self._ticket_seq, client_id=cid)
            self._wait_tickets.append(ticket)
            self._client_queued[cid] = self._client_queued.get(cid, 0) + 1
            try:
                while True:
                    with self._lock:
                        rec = self._clients.get(cid)
                        if rec is None:
                            raise DistributionAuthError("客户端密钥已被吊销。", status_code=401, code="invalid_api_key")
                        if not self.external_access_enabled or not rec.enabled:
                            raise DistributionAuthError("客户端接入已被暂停。", status_code=403, code="client_disabled")
                        self._check_daily_quota_locked(rec)
                        pool_cap = int(capacity_provider()) if capacity_provider is not None else 0
                        ext_cap = self._compute_external_cap(pool_cap)
                        cur_client_inf = self._client_in_flight.get(cid, 0)
                        can_enter = (
                            self._external_in_flight < ext_cap
                            and cur_client_inf < rec.max_concurrency
                            and self._is_fair_winner_locked(ticket)
                        )
                        if can_enter:
                            self._consume_rpm_slot_locked(rec, time.monotonic())
                            self._external_in_flight += 1
                            self._client_in_flight[cid] = cur_client_inf + 1
                            rec.last_used_at = int(time.time())
                            if principal.client_ip:
                                rec.last_ip = principal.client_ip
                            admitted = True
                            break

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DistributionQuotaError(
                            f"当前共享工作池繁忙或已达客户端并发上限，排队等待超时（>{eff_timeout:.0f}s）。",
                            status_code=429,
                            code="concurrency_timeout",
                        )
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=min(remaining, 2.0))
                    except asyncio.TimeoutError:
                        continue
            finally:
                if ticket in self._wait_tickets:
                    self._wait_tickets.remove(ticket)
                new_q = max(0, self._client_queued.get(cid, 0) - 1)
                if new_q:
                    self._client_queued[cid] = new_q
                else:
                    self._client_queued.pop(cid, None)
                cond.notify_all()

        try:
            yield
        finally:
            if admitted:
                async with cond:
                    self._external_in_flight = max(0, self._external_in_flight - 1)
                    rem = max(0, self._client_in_flight.get(cid, 0) - 1)
                    if rem:
                        self._client_in_flight[cid] = rem
                    else:
                        self._client_in_flight.pop(cid, None)
                    cond.notify_all()

    def record_usage(
        self,
        principal: RequestPrincipal,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: bool = False,
    ) -> None:
        if principal.is_host or not principal.client_id:
            return
        total_toks = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
        with self._lock:
            rec = self._clients.get(principal.client_id)
            if rec is None:
                return
            rec.refresh_day()
            rec.last_used_at = int(time.time())
            if principal.client_ip:
                rec.last_ip = principal.client_ip
            if error:
                rec.total_errors += 1
            else:
                rec.today_requests += 1
                rec.total_requests += 1
                rec.today_tokens += total_toks
                rec.total_tokens += total_toks
            self._save_locked()

    def snapshot(
        self,
        *,
        include_secrets: bool = False,
        pool_capacity: int = 0,
    ) -> Dict[str, Any]:
        with self._lock:
            ext_cap = self._compute_external_cap(pool_capacity) if pool_capacity > 0 else 0
            clients_list = [
                rec.to_view_dict(
                    include_secret=include_secrets,
                    in_flight=self._client_in_flight.get(rec.client_id, 0),
                    queued=self._client_queued.get(rec.client_id, 0),
                )
                for rec in sorted(self._clients.values(), key=lambda r: r.created_at, reverse=True)
            ]
            return {
                "policy": {
                    "external_access_enabled": self.external_access_enabled,
                    "host_reserved_slots": self.host_reserved_slots,
                    "external_queue_timeout_seconds": self.external_queue_timeout_seconds,
                    "public_base_url": self.public_base_url,
                },
                "runtime": {
                    "host_in_flight": self._host_in_flight,
                    "external_in_flight": self._external_in_flight,
                    "external_queued": sum(self._client_queued.values()),
                    "pool_capacity": pool_capacity,
                    "external_parallel_cap": ext_cap,
                    "active_client_keys": sum(1 for c in self._clients.values() if c.enabled),
                    "total_client_keys": len(self._clients),
                },
                "clients": clients_list,
            }

"""Public API distribution, multi-client authentication, tenant isolation, and host-first traffic scheduling."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
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

from platform_compat import SHANGHAI_TZ, atomic_write_json, billing_day_key
from schemas import normalize_default_model, parse_model_and_session, resolve_target_web_model

logger = logging.getLogger("gemini_bridge.distribution")

HOST_TENANT_ID = "host"
DEFAULT_TZ = SHANGHAI_TZ

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

    def __init__(
        self,
        message: str,
        status_code: int = 401,
        code: str = "invalid_api_key",
        principal: Optional["RequestPrincipal"] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.principal = principal


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
    if "%" in val:
        val = val.split("%", 1)[0]
    return val


def _is_loopback_peer(peer_ip: str) -> bool:
    if peer_ip in _LOCAL_PEER_HOSTS:
        return True
    if not peer_ip:
        return False
    try:
        return ipaddress.ip_address(peer_ip).is_loopback
    except ValueError:
        return False


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
    if not _is_loopback_peer(peer_ip):
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
    """Return current daily quota window key (resets at 00:00 PT / 16:00 PST Shanghai)."""
    return billing_day_key()


def _ip_matches_allowlist(client_ip: str, allowed_ips: Sequence[str]) -> bool:
    if not allowed_ips:
        return True
    ip_str = (client_ip or "").strip()
    if not ip_str or ip_str == "unknown":
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        addr = None
    for raw_rule in allowed_ips:
        rule = (raw_rule or "").strip()
        if not rule:
            continue
        if rule == "*" or rule.lower() == ip_str.lower():
            return True
        if addr is not None:
            try:
                if "/" in rule:
                    if addr in ipaddress.ip_network(rule, strict=False):
                        return True
                elif addr == ipaddress.ip_address(rule):
                    return True
            except ValueError:
                continue
    return False


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
    daily_cost_limit_usd: float = 0.0
    total_token_limit: int = 0
    expires_at: int = 0
    allowed_models: List[str] = field(default_factory=list)
    allowed_ips: List[str] = field(default_factory=list)
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_used_at: int = 0
    last_ip: str = ""
    last_model: str = ""
    last_session_id: str = ""
    stats_day: str = field(default_factory=_today_shanghai)
    today_requests: int = 0
    today_tokens: int = 0
    today_prompt_tokens: int = 0
    today_completion_tokens: int = 0
    today_cost_usd: float = 0.0
    today_errors: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    total_errors: int = 0

    def is_expired(self, now_ts: Optional[int] = None) -> bool:
        if self.expires_at <= 0:
            return False
        cur = int(time.time()) if now_ts is None else int(now_ts)
        return cur >= self.expires_at

    def refresh_day(self, today: Optional[str] = None) -> None:
        cur = today or _today_shanghai()
        if self.stats_day != cur:
            self.stats_day = cur
            self.today_requests = 0
            self.today_tokens = 0
            self.today_prompt_tokens = 0
            self.today_completion_tokens = 0
            self.today_cost_usd = 0.0
            self.today_errors = 0

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
            "daily_cost_limit_usd": round(max(0.0, float(self.daily_cost_limit_usd or 0.0)), 6),
            "total_token_limit": self.total_token_limit,
            "expires_at": self.expires_at,
            "allowed_models": list(self.allowed_models),
            "allowed_ips": list(self.allowed_ips),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_ip": self.last_ip,
            "last_model": self.last_model,
            "last_session_id": self.last_session_id,
            "stats_day": self.stats_day,
            "today_requests": self.today_requests,
            "today_tokens": self.today_tokens,
            "today_prompt_tokens": self.today_prompt_tokens,
            "today_completion_tokens": self.today_completion_tokens,
            "today_cost_usd": round(max(0.0, float(self.today_cost_usd or 0.0)), 6),
            "today_errors": self.today_errors,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_usd": round(max(0.0, float(self.total_cost_usd or 0.0)), 6),
            "total_errors": self.total_errors,
        }

    def to_view_dict(self, *, include_secret: bool = False, in_flight: int = 0, queued: int = 0) -> Dict[str, Any]:
        self.refresh_day()
        expired = self.is_expired()
        out: Dict[str, Any] = {
            "client_id": self.client_id,
            "tenant_id": f"client:{self.client_id}",
            "name": self.name,
            "note": self.note,
            "key_preview": _mask_key(self.api_key),
            "enabled": self.enabled,
            "expired": expired,
            "max_concurrency": self.max_concurrency,
            "rpm_limit": self.rpm_limit,
            "daily_request_limit": self.daily_request_limit,
            "daily_token_limit": self.daily_token_limit,
            "daily_cost_limit_usd": round(max(0.0, float(self.daily_cost_limit_usd or 0.0)), 4),
            "total_token_limit": self.total_token_limit,
            "expires_at": self.expires_at,
            "allowed_models": list(self.allowed_models),
            "allowed_ips": list(self.allowed_ips),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_ip": self.last_ip,
            "last_model": self.last_model,
            "last_session_id": self.last_session_id,
            "today_requests": self.today_requests,
            "today_tokens": self.today_tokens,
            "today_prompt_tokens": self.today_prompt_tokens,
            "today_completion_tokens": self.today_completion_tokens,
            "today_cost_usd": round(max(0.0, float(self.today_cost_usd or 0.0)), 6),
            "today_errors": self.today_errors,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_usd": round(max(0.0, float(self.total_cost_usd or 0.0)), 6),
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
    aborted: bool = False


@dataclass
class LiveTaskEntry:
    task_id: str
    tenant_id: str
    client_id: Optional[str]
    tenant_name: str
    client_ip: str
    model: str
    task_type: str = "chat"
    session_id: str = ""
    slot_key: str = ""
    tab_id: Optional[int] = None
    prompt_preview: str = ""
    stream: bool = False
    phase: str = "queued"
    started_at: float = field(default_factory=time.time)

    def to_dict(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        cur = time.time() if now_ts is None else now_ts
        return {
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "tenant_name": self.tenant_name,
            "client_ip": self.client_ip,
            "model": self.model,
            "task_type": self.task_type,
            "session_id": self.session_id,
            "slot_key": self.slot_key,
            "tab_id": self.tab_id,
            "prompt_preview": self.prompt_preview,
            "stream": self.stream,
            "phase": self.phase,
            "state": self.phase,
            "started_at": int(self.started_at),
            "elapsed_ms": max(0, int((cur - self.started_at) * 1000)),
        }


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
        self._live_tasks: Dict[str, LiveTaskEntry] = {}

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
                daily_cost_limit_usd=max(0.0, float(item.get("daily_cost_limit_usd") or 0.0)),
                total_token_limit=max(0, int(item.get("total_token_limit") or 0)),
                expires_at=max(0, int(item.get("expires_at") or 0)),
                allowed_models=[
                    str(m).strip() for m in (item.get("allowed_models") or []) if str(m).strip()
                ],
                allowed_ips=[
                    str(ip).strip() for ip in (item.get("allowed_ips") or []) if str(ip).strip()
                ],
                created_at=int(item.get("created_at") or time.time()),
                last_used_at=int(item.get("last_used_at") or 0),
                last_ip=str(item.get("last_ip") or ""),
                last_model=str(item.get("last_model") or ""),
                last_session_id=str(item.get("last_session_id") or ""),
                stats_day=str(item.get("stats_day") or today),
                today_requests=max(0, int(item.get("today_requests") or 0)),
                today_tokens=max(0, int(item.get("today_tokens") or 0)),
                today_prompt_tokens=max(0, int(item.get("today_prompt_tokens") or 0)),
                today_completion_tokens=max(0, int(item.get("today_completion_tokens") or 0)),
                today_cost_usd=max(0.0, float(item.get("today_cost_usd") or 0.0)),
                today_errors=max(0, int(item.get("today_errors") or 0)),
                total_requests=max(0, int(item.get("total_requests") or 0)),
                total_tokens=max(0, int(item.get("total_tokens") or 0)),
                total_prompt_tokens=max(0, int(item.get("total_prompt_tokens") or 0)),
                total_completion_tokens=max(0, int(item.get("total_completion_tokens") or 0)),
                total_cost_usd=max(0.0, float(item.get("total_cost_usd") or 0.0)),
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
            atomic_write_json(self._storage_path, payload, mode=0o600, dir_mode=0o700)
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
        daily_cost_limit_usd: float = 0.0,
        total_token_limit: int = 0,
        expires_at: int = 0,
        allowed_models: Optional[Sequence[str]] = None,
        allowed_ips: Optional[Sequence[str]] = None,
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
                daily_cost_limit_usd=max(0.0, float(daily_cost_limit_usd or 0.0)),
                total_token_limit=max(0, int(total_token_limit or 0)),
                expires_at=max(0, int(expires_at or 0)),
                allowed_models=[str(m).strip() for m in (allowed_models or []) if str(m).strip()],
                allowed_ips=[str(ip).strip() for ip in (allowed_ips or []) if str(ip).strip()],
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
        daily_cost_limit_usd: Optional[float] = None,
        total_token_limit: Optional[int] = None,
        expires_at: Optional[int] = None,
        allowed_models: Optional[Sequence[str]] = None,
        allowed_ips: Optional[Sequence[str]] = None,
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
            if daily_cost_limit_usd is not None:
                rec.daily_cost_limit_usd = max(0.0, float(daily_cost_limit_usd))
            if total_token_limit is not None:
                rec.total_token_limit = max(0, int(total_token_limit))
            if expires_at is not None:
                rec.expires_at = max(0, int(expires_at))
            if allowed_models is not None:
                rec.allowed_models = [str(m).strip() for m in allowed_models if str(m).strip()]
            if allowed_ips is not None:
                rec.allowed_ips = [str(ip).strip() for ip in allowed_ips if str(ip).strip()]
            if enabled is False:
                for t in self._wait_tickets:
                    if t.client_id == client_id:
                        t.aborted = True
            self._save_locked()
            return rec.to_view_dict(
                include_secret=True,
                in_flight=self._client_in_flight.get(client_id, 0),
                queued=self._client_queued.get(client_id, 0),
            )

    def reset_client_quota(self, client_id: str, *, reset_total: bool = False) -> Dict[str, Any]:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec is None:
                raise KeyError(f"未找到客户端：{client_id}")
            rec.stats_day = _today_shanghai()
            rec.today_requests = 0
            rec.today_tokens = 0
            rec.today_prompt_tokens = 0
            rec.today_completion_tokens = 0
            rec.today_cost_usd = 0.0
            rec.today_errors = 0
            self._rpm_windows.pop(client_id, None)
            if reset_total:
                rec.total_requests = 0
                rec.total_tokens = 0
                rec.total_prompt_tokens = 0
                rec.total_completion_tokens = 0
                rec.total_cost_usd = 0.0
                rec.total_errors = 0
            self._save_locked()
            return rec.to_view_dict(
                include_secret=True,
                in_flight=self._client_in_flight.get(client_id, 0),
                queued=self._client_queued.get(client_id, 0),
            )

    async def abort_client_waiters(self, client_id: str) -> int:
        aborted_cnt = 0
        with self._lock:
            for t in self._wait_tickets:
                if t.client_id == client_id and not t.aborted:
                    t.aborted = True
                    aborted_cnt += 1
        cond = self._get_condition()
        async with cond:
            cond.notify_all()
        return aborted_cnt

    def register_live_task(
        self,
        task_id: str,
        principal: RequestPrincipal,
        *,
        model: str,
        task_type: str = "chat",
        session_id: str = "",
        prompt_preview: str = "",
        stream: bool = False,
        phase: str = "queued",
        state: Optional[str] = None,
    ) -> None:
        eff_phase = state if state is not None else phase
        t_label = "本机直连" if principal.is_host else f"客户端 · {principal.name}"
        with self._lock:
            self._live_tasks[task_id] = LiveTaskEntry(
                task_id=task_id,
                tenant_id=principal.tenant_id,
                client_id=principal.client_id,
                tenant_name=t_label,
                client_ip=principal.client_ip,
                model=model,
                task_type=task_type,
                session_id=session_id,
                prompt_preview=(prompt_preview or "").strip()[:140],
                stream=bool(stream),
                phase=eff_phase,
            )

    def update_live_task(
        self,
        task_id: str,
        *,
        phase: Optional[str] = None,
        state: Optional[str] = None,
        slot_key: Optional[str] = None,
        tab_id: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> None:
        eff_phase = state if state is not None else phase
        with self._lock:
            entry = self._live_tasks.get(task_id)
            if entry is None:
                return
            if eff_phase is not None:
                entry.phase = eff_phase
            if slot_key is not None:
                entry.slot_key = slot_key
            if tab_id is not None:
                entry.tab_id = tab_id
            if session_id is not None:
                entry.session_id = session_id

    def finish_live_task(self, task_id: str) -> None:
        with self._lock:
            self._live_tasks.pop(task_id, None)

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
            for t in self._wait_tickets:
                if t.client_id == client_id:
                    t.aborted = True
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
                    matched_principal = RequestPrincipal(
                        tenant_id=f"client:{matched.client_id}",
                        client_id=matched.client_id,
                        name=matched.name,
                        is_host=False,
                        is_direct_local=direct_local,
                        client_ip=client_ip,
                    )
                    if not self.external_access_enabled:
                        raise DistributionAuthError(
                            "主机管理员已临时暂停外部客户端接入。",
                            status_code=403,
                            code="external_access_paused",
                            principal=matched_principal,
                        )
                    if not matched.enabled:
                        raise DistributionAuthError(
                            f"客户端密钥（{matched.name}）已被暂停使用，请联系主机管理员。",
                            status_code=403,
                            code="client_disabled",
                            principal=matched_principal,
                        )
                    if matched.is_expired():
                        raise DistributionAuthError(
                            f"客户端密钥（{matched.name}）已超过有效期，请联系主机管理员续期。",
                            status_code=403,
                            code="client_expired",
                            principal=matched_principal,
                        )
                    if matched.allowed_ips and not _ip_matches_allowlist(client_ip, matched.allowed_ips):
                        raise DistributionAuthError(
                            f"当前来源 IP ({client_ip}) 未在客户端密钥白名单内。",
                            status_code=403,
                            code="ip_not_allowed",
                            principal=matched_principal,
                        )
                    return matched_principal
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
        raw = normalize_default_model(requested_model)
        base_model, _ = parse_model_and_session(raw)
        target_web = resolve_target_web_model(base_model)
        allowed_norm = {normalize_default_model(m).lower() for m in rec.allowed_models if m.strip()}
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
        if rec.daily_cost_limit_usd > 0 and rec.today_cost_usd >= rec.daily_cost_limit_usd:
            raise DistributionQuotaError(
                f"已达到今日额度预算上限（${rec.daily_cost_limit_usd:.2f}/日），请联系管理员调整配额。",
                status_code=429,
                code="daily_quota_exceeded",
            )
        if rec.total_token_limit > 0 and rec.total_tokens >= rec.total_token_limit:
            raise DistributionQuotaError(
                f"已达到该密钥累计总 Token 封顶上限（{rec.total_token_limit:,} Tokens），请联系管理员扩容。",
                status_code=429,
                code="total_quota_exceeded",
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
            if candidate.aborted:
                continue
            crec = self._clients.get(candidate.client_id)
            if crec is None or not crec.enabled or crec.is_expired():
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
            if rec.is_expired():
                raise DistributionAuthError("客户端密钥已超过有效期。", status_code=403, code="client_expired")
            if rec.allowed_ips and not _ip_matches_allowlist(principal.client_ip, rec.allowed_ips):
                raise DistributionAuthError("来源 IP 不在允许白名单内。", status_code=403, code="ip_not_allowed")
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
            with self._lock:
                self._host_waiting += 1
            async with cond:
                with self._lock:
                    self._host_waiting = max(0, self._host_waiting - 1)
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
            if rec.is_expired():
                raise DistributionAuthError("客户端密钥已超过有效期。", status_code=403, code="client_expired")
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
                        if ticket.aborted:
                            raise DistributionAuthError("管理员已中断当前客户端排队请求。", status_code=403, code="client_aborted")
                        rec = self._clients.get(cid)
                        if rec is None:
                            raise DistributionAuthError("客户端密钥已被吊销。", status_code=401, code="invalid_api_key")
                        if not self.external_access_enabled or not rec.enabled or rec.is_expired():
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
                            if requested_model:
                                rec.last_model = str(requested_model)[:64]
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
        cost_usd: float = 0.0,
        model: str = "",
        session_id: str = "",
        error: bool = False,
    ) -> None:
        if principal.is_host or not principal.client_id:
            return
        p_toks = max(0, int(prompt_tokens or 0))
        c_toks = max(0, int(completion_tokens or 0))
        total_toks = p_toks + c_toks
        eff_cost = max(0.0, float(cost_usd or 0.0))
        with self._lock:
            rec = self._clients.get(principal.client_id)
            if rec is None:
                return
            rec.refresh_day()
            rec.last_used_at = int(time.time())
            if principal.client_ip:
                rec.last_ip = principal.client_ip
            if model:
                rec.last_model = str(model)[:64]
            if session_id:
                rec.last_session_id = str(session_id)[:80]
            if error:
                rec.today_errors += 1
                rec.total_errors += 1
            else:
                rec.today_requests += 1
                rec.total_requests += 1
                rec.today_tokens += total_toks
                rec.total_tokens += total_toks
                rec.today_prompt_tokens += p_toks
                rec.today_completion_tokens += c_toks
                rec.today_cost_usd = round(rec.today_cost_usd + eff_cost, 8)
                rec.total_prompt_tokens += p_toks
                rec.total_completion_tokens += c_toks
                rec.total_cost_usd = round(rec.total_cost_usd + eff_cost, 8)
            self._save_locked()

    def snapshot(
        self,
        *,
        include_secrets: bool = False,
        pool_capacity: int = 0,
    ) -> Dict[str, Any]:
        with self._lock:
            now_ts = time.time()
            ext_cap = self._compute_external_cap(pool_capacity) if pool_capacity > 0 else 0
            clients_list = [
                rec.to_view_dict(
                    include_secret=include_secrets,
                    in_flight=self._client_in_flight.get(rec.client_id, 0),
                    queued=self._client_queued.get(rec.client_id, 0),
                )
                for rec in sorted(self._clients.values(), key=lambda r: r.created_at, reverse=True)
            ]
            live_tasks_list = [
                t.to_dict(now_ts)
                for t in sorted(self._live_tasks.values(), key=lambda x: x.started_at, reverse=True)
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
                    "host_waiting": self._host_waiting,
                    "external_in_flight": self._external_in_flight,
                    "external_queued": sum(self._client_queued.values()),
                    "pool_capacity": pool_capacity,
                    "external_parallel_cap": ext_cap,
                    "active_client_keys": sum(1 for c in self._clients.values() if c.enabled and not c.is_expired(int(now_ts))),
                    "total_client_keys": len(self._clients),
                    "live_task_count": len(live_tasks_list),
                },
                "clients": clients_list,
                "live_tasks": live_tasks_list,
            }

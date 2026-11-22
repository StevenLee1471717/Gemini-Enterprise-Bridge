"""Multi-Tab Parallel Browser Bridge Hub with Sticky Session Routing, Auto-Spawn, and Live Streaming."""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import logging
from pathlib import Path
import re
import secrets
import time
import urllib.parse
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Set

from fastapi import WebSocket

from context_builder import extract_vertex_session_id
from updater import compare_semver, normalize_version
from version import APP_VERSION

logger = logging.getLogger("gemini_bridge.hub")


class BridgeUnavailableError(RuntimeError):
    """Raised when no active Gemini Enterprise browser client is connected."""


class BridgeTimeoutError(TimeoutError):
    """Raised when waiting for a worker tab or browser reply times out."""


class BridgeExecutionError(RuntimeError):
    """Raised when the browser driver reports an execution failure."""


class _DynamicParallelLimiter:
    """FIFO async concurrency gate supporting runtime cap changes (1..200)."""

    def __init__(self, cap_getter: Callable[[], int]) -> None:
        self._cap_getter = cap_getter
        self._in_use = 0
        self._waiters: collections.deque[asyncio.Future[None]] = collections.deque()

    def _can_acquire(self) -> bool:
        cap = max(1, int(self._cap_getter()))
        return self._in_use < cap

    def wake_waiters(self) -> None:
        while self._waiters and self._can_acquire():
            fut = self._waiters.popleft()
            if not fut.done():
                self._in_use += 1
                fut.set_result(None)

    async def acquire(self) -> bool:
        if not self._waiters and self._can_acquire():
            self._in_use += 1
            return True
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
            return True
        except BaseException:
            with contextlib.suppress(ValueError):
                self._waiters.remove(fut)
            if fut.done() and not fut.cancelled():
                self._in_use -= 1
            self.wake_waiters()
            raise

    def release(self) -> None:
        self._in_use = max(0, self._in_use - 1)
        self.wake_waiters()


def make_slot_key(client_id: str, tab_id: Optional[int]) -> str:
    return f"{client_id}:{tab_id}" if tab_id is not None else f"{client_id}:default"


def derive_clean_vertex_url(raw_url: str) -> str:
    """Strip `/r/session/<id>` or `/session/<id>` so spawned tabs open a clean chat in the same workspace."""
    if not raw_url:
        return "https://vertexaisearch.cloud.google.com/"
    cleaned = re.sub(r"/r/session/[^/?#]+[^?#]*", "", raw_url, flags=re.IGNORECASE)
    cleaned = re.sub(r"/session/[^/?#]+[^?#]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"/c/[^/?#]+[^?#]*", "", cleaned, flags=re.IGNORECASE)
    return cleaned or "https://vertexaisearch.cloud.google.com/"


@dataclass
class WorkerSlot:
    """Represents a single independent browser tab capable of executing prompts in parallel."""

    slot_key: str
    client_id: str
    tab_id: Optional[int] = None
    tab_url: str = ""
    tab_title: str = ""
    tab_ready: bool = True
    active_model: str = ""
    captured_rpc: bool = False
    completed_requests: int = 0
    last_used_at: float = 0.0
    pinned_session_id: Optional[str] = None
    owner_tenant_id: Optional[str] = None
    pending_tool_call_until: float = 0.0
    in_flight_request_id: Optional[str] = None
    waiting_count: int = 0
    last_dispatch_submitted: bool = False
    last_dispatch_tab_reset: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seat_index: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    tab_busy_until: float = 0.0

    @property
    def is_busy(self) -> bool:
        return self.lock.locked() or self.in_flight_request_id is not None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "client_id": self.client_id,
            "tab_id": self.tab_id,
            "seat_index": self.seat_index,
            "tab_url": self.tab_url,
            "tab_title": self.tab_title,
            "tab_ready": self.tab_ready,
            "active_model": self.active_model,
            "captured_rpc": self.captured_rpc,
            "busy": self.is_busy,
            "waiting_count": self.waiting_count,
            "pinned_session_id": self.pinned_session_id,
            "owner_tenant_id": self.owner_tenant_id,
            "pending_tool_call_until": round(self.pending_tool_call_until, 3) if self.pending_tool_call_until > 0 else 0.0,
            "completed_requests": self.completed_requests,
            "last_used_at": int(self.last_used_at) if self.last_used_at else 0,
        }


@dataclass
class BrowserClientState:
    client_id: str
    transport: str  # "websocket" | "http_poll"
    websocket: Optional[WebSocket] = None
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    user_agent: str = ""
    tab_id: Optional[int] = None
    tab_url: str = ""
    tab_title: str = ""
    tab_ready: bool = True
    driver_mode: str = "auto"
    captured_rpc: bool = False
    active_model: str = ""
    extension_version: str = ""
    notified_reload_for_version: str = ""
    warned_outdated_backend_for: str = ""
    completed_requests: int = 0
    capabilities: Set[str] = field(default_factory=set)
    client_max_parallel_tabs: Optional[int] = None
    spawning_count: int = 0
    disconnected_at: float = 0.0
    pending_cancels: List[tuple[str, Optional[int], bool]] = field(default_factory=list)
    ws_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    command_queue: asyncio.Queue[Dict[str, Any]] = field(default_factory=asyncio.Queue)

    def is_alive(self, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.time()
        if self.transport == "websocket":
            return self.websocket is not None
        # HTTP long-poll Tampermonkey clients expire after 45s of silence
        return (ts - self.last_seen_at) <= 45.0

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "transport": self.transport,
            "connected_at": int(self.connected_at),
            "last_seen_at": int(self.last_seen_at),
            "tab_id": self.tab_id,
            "tab_url": self.tab_url,
            "tab_title": self.tab_title,
            "tab_ready": self.tab_ready,
            "driver_mode": self.driver_mode,
            "captured_rpc": self.captured_rpc,
            "active_model": self.active_model,
            "extension_version": self.extension_version,
            "completed_requests": self.completed_requests,
            "capabilities": sorted(self.capabilities),
            "max_parallel_tabs": self.client_max_parallel_tabs,
        }


@dataclass
class InFlightRequest:
    request_id: str
    client_id: str
    slot_key: str
    tab_id: Optional[int]
    prompt: str
    reset_chat: bool
    stream: bool
    target_model: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    events: asyncio.Queue[Dict[str, Any]] = field(default_factory=asyncio.Queue)
    reply_chunks: Dict[int, str] = field(default_factory=dict)
    expected_reply_chunks: int = 0
    submitted: bool = False
    tab_reset: bool = False


PROMPT_CHUNK_THRESHOLD = 32768
PROMPT_CHUNK_SIZE = 24576
ATTACHMENT_STAGE_TTL_SECONDS = 900.0


@dataclass
class StagedAttachment:
    attachment_id: str
    token: str
    filename: str
    mime_type: str
    data: bytes
    sha256: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


class BrowserBridgeHub:
    """Coordinates parallel browser worker tabs, sticky session routing, auto-spawning, and live streaming."""

    def __init__(
        self,
        include_citations: bool = True,
        callback_base_url: str = "",
        max_parallel_tabs: int = 10,
        auto_spawn_tabs: bool = True,
        session_affinity_ttl_seconds: float = 300.0,
        backend_version: str = APP_VERSION,
        ws_reconnect_grace_seconds: float = 45.0,
        ws_reconnect_wait_seconds: float = 35.0,
    ) -> None:
        self.include_citations = include_citations
        self.callback_base_url = callback_base_url.rstrip("/")
        parsed_cap = int(max_parallel_tabs)
        self.max_parallel_tabs = 10 if parsed_cap <= 0 else min(200, parsed_cap)
        self.auto_spawn_tabs = auto_spawn_tabs
        self.session_affinity_ttl_seconds = max(0.0, session_affinity_ttl_seconds)
        self.backend_version = normalize_version(backend_version) or APP_VERSION
        self.ws_reconnect_grace_seconds = max(0.0, float(ws_reconnect_grace_seconds))
        self.ws_reconnect_wait_seconds = max(0.0, float(ws_reconnect_wait_seconds))
        self._ws_reconnect_event = asyncio.Event()
        self._ws_grace_tasks: Dict[str, asyncio.Task[None]] = {}
        self._cached_ws_usage_snapshot: Optional[Dict[str, Any]] = None
        self._cached_ws_usage_at: float = 0.0
        bg_path = Path(__file__).resolve().parent / "background.js"
        drv_match = (
            re.search(r'EXPECTED_DRIVER_VERSION\s*=\s*"([^"]+)"', bg_path.read_text(encoding="utf-8", errors="ignore"))
            if bg_path.is_file()
            else None
        )
        self.expected_driver_version = normalize_version(drv_match.group(1)) if drv_match else ""

        self._clients: Dict[str, BrowserClientState] = {}
        self._slots: Dict[str, WorkerSlot] = {}
        self._session_affinity: Dict[str, str] = {}  # explicit_session_id -> slot_key
        self._pending: Dict[str, InFlightRequest] = {}
        self._pending_spawns: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._pending_resets: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._pending_opens: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._staged_attachments: Dict[str, StagedAttachment] = {}
        self._routing_lock = asyncio.Lock()
        self._thread_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._parallel_limit = _DynamicParallelLimiter(self._effective_global_tab_cap)

        self._total_requests = 0
        self._total_errors = 0
        self._recent_logs: List[Dict[str, Any]] = []
        self._discovered_web_models: List[str] = []
        self.usage_snapshot_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.on_slot_removed: Optional[Callable[[str], None]] = None
        self.on_slot_migrated: Optional[Callable[[str, str], None]] = None
        self.on_slot_evicted: Optional[Callable[[WorkerSlot], None]] = None
        self.has_slot_conversation: Optional[Callable[[str], bool]] = None
        self.is_slot_ephemeral_probe: Optional[Callable[[str], bool]] = None
        self.is_slot_session_active: Optional[Callable[[WorkerSlot], bool]] = None
        self.on_extension_version_reported: Optional[Callable[[str], Any]] = None
        self.on_outdated_extension_detected: Optional[Callable[[str], Any]] = None

    @property
    def discovered_web_models(self) -> List[str]:
        return list(self._discovered_web_models)

    def record_discovered_models(
        self,
        models: Optional[Sequence[Any]] = None,
        active_model: Optional[str] = None,
    ) -> None:
        """Record live model labels observed in the Gemini Enterprise browser UI."""
        seen_lower = {m.lower() for m in self._discovered_web_models}
        items: List[Any] = list(models) if models else []
        if active_model:
            items.append(active_model)
        for raw in items:
            if not isinstance(raw, str):
                continue
            label = raw.splitlines()[0].strip()
            if not label or len(label) > 80:
                continue
            if label.lower() in {"default", "auto", "自动", "model", "choose a model", "select a model"}:
                continue
            if label.lower() not in seen_lower:
                self._discovered_web_models.append(label)
                seen_lower.add(label.lower())
                if len(self._discovered_web_models) > 64:
                    removed = self._discovered_web_models.pop(0)
                    seen_lower.discard(removed.lower())

    def record_reply_error(self, slot: WorkerSlot, message: str) -> None:
        """A transport-complete reply can still be rejected by the tool protocol."""
        self._total_errors += 1
        self.add_log("error", message, slot.client_id)

    def add_log(self, level: str, message: str, client_id: Optional[str] = None) -> None:
        lvl = (level or "info").lower()
        entry = {
            "ts": int(time.time()),
            "level": lvl,
            "client_id": client_id,
            "message": message[:1000],
        }
        self._recent_logs.append(entry)
        if len(self._recent_logs) > 250:
            self._recent_logs = self._recent_logs[-250:]
        prefix = f"[{client_id}] " if client_id else ""
        if lvl == "error":
            logger.error("%s%s", prefix, message)
        elif lvl in {"warn", "warning"}:
            logger.warning("%s%s", prefix, message)
        else:
            logger.debug("%s%s", prefix, message)

    # ------------------------------------------------------------------
    # Worker Slot Synchronization
    # ------------------------------------------------------------------

    def _upsert_single_slot(
        self,
        client_id: str,
        tab_id: Optional[int],
        tab_url: str = "",
        tab_title: str = "",
        tab_ready: bool = True,
        active_model: str = "",
        captured_rpc: Optional[bool] = None,
    ) -> WorkerSlot:
        skey = make_slot_key(client_id, tab_id)
        default_key = make_slot_key(client_id, None)

        # Upgrade placeholder `:default` slot once concrete `tab_id` arrives
        if tab_id is not None and skey not in self._slots and default_key in self._slots:
            migrated = self._slots.pop(default_key)
            migrated.slot_key = skey
            migrated.tab_id = tab_id
            self._slots[skey] = migrated
            for sid, mapped_key in list(self._session_affinity.items()):
                if mapped_key == default_key:
                    self._session_affinity[sid] = skey
            if self.on_slot_migrated is not None:
                try:
                    self.on_slot_migrated(default_key, skey)
                except Exception:
                    pass

        slot = self._slots.get(skey)
        if slot is None:
            slot = WorkerSlot(
                slot_key=skey,
                client_id=client_id,
                tab_id=tab_id,
                tab_url=tab_url,
                tab_title=tab_title,
                tab_ready=tab_ready,
                active_model=active_model,
                captured_rpc=bool(captured_rpc),
            )
            self._slots[skey] = slot
        else:
            if tab_url:
                prev_vsid = extract_vertex_session_id(slot.tab_url)
                next_vsid = extract_vertex_session_id(tab_url)
                if prev_vsid and next_vsid != prev_vsid and not slot.is_busy:
                    self._evict_slot_binding(slot)
                slot.tab_url = tab_url
            if tab_title:
                slot.tab_title = tab_title
            slot.tab_ready = tab_ready
            if active_model:
                slot.active_model = active_model
            if captured_rpc is not None:
                slot.captured_rpc = bool(captured_rpc)
        if active_model:
            self.record_discovered_models(active_model=active_model)
        return slot

    def _evict_slot_binding(self, slot: WorkerSlot) -> None:
        """Archive the slot's current session (if any) and clear its L1 session binding."""
        if self.on_slot_evicted is not None:
            try:
                self.on_slot_evicted(slot)
            except Exception:
                pass
        old_sid = slot.pinned_session_id
        slot.pinned_session_id = None
        slot.owner_tenant_id = None
        slot.pending_tool_call_until = 0.0
        if old_sid and self._session_affinity.get(old_sid) == slot.slot_key:
            self._session_affinity.pop(old_sid, None)
        for sid, mapped in list(self._session_affinity.items()):
            if mapped == slot.slot_key:
                self._session_affinity.pop(sid, None)

    def evict_tenant_slots(self, tenant_id: str, *, abort_inflight: bool = False) -> Dict[str, int]:
        eff_tid = (tenant_id or "").strip()
        if not eff_tid:
            return {"evicted_slots": 0, "aborted_requests": 0}
        evicted_slots = 0
        aborted_reqs = 0
        prefix = f"{eff_tid}::"
        for sid in list(self._session_affinity.keys()):
            if sid.startswith(prefix):
                self._session_affinity.pop(sid, None)
        for slot in list(self._slots.values()):
            slot_belongs = slot.owner_tenant_id == eff_tid or (
                bool(slot.pinned_session_id) and slot.pinned_session_id.startswith(prefix)
            )
            if not slot_belongs:
                continue
            if abort_inflight:
                for req_id, inflight in list(self._pending.items()):
                    if inflight.slot_key == slot.slot_key:
                        inflight.events.put_nowait(
                            {
                                "type": "error",
                                "message": "管理员已断开或重置当前客户端会话连接。",
                            }
                        )
                        self._pending.pop(req_id, None)
                        aborted_reqs += 1
            if not slot.is_busy or abort_inflight:
                self._evict_slot_binding(slot)
                evicted_slots += 1
        return {"evicted_slots": evicted_slots, "aborted_requests": aborted_reqs}

    def clear_slot_affinity_only(self, slot: WorkerSlot) -> None:
        old_sid = slot.pinned_session_id
        slot.pinned_session_id = None
        slot.owner_tenant_id = None
        slot.pending_tool_call_until = 0.0
        if old_sid and self._session_affinity.get(old_sid) == slot.slot_key:
            self._session_affinity.pop(old_sid, None)
        for sid, mapped in list(self._session_affinity.items()):
            if mapped == slot.slot_key:
                self._session_affinity.pop(sid, None)

    def restore_slot_affinity(
        self,
        slot_key: str,
        explicit_session_id: Optional[str],
        last_used_at: float = 0.0,
        owner_tenant_id: Optional[str] = None,
    ) -> None:
        slot = self._slots.get(slot_key)
        if slot is None:
            return
        if owner_tenant_id:
            slot.owner_tenant_id = owner_tenant_id
        if last_used_at and last_used_at > 0.0:
            slot.last_used_at = max(slot.last_used_at, float(last_used_at))
        if not explicit_session_id:
            return
        sid = explicit_session_id.strip()
        if not sid:
            return
        slot.pinned_session_id = sid
        self._session_affinity[sid] = slot_key

    def _remove_slot(self, slot_key: str, reason: str = "tab_closed") -> None:
        self._slots.pop(slot_key, None)
        if self.on_slot_removed is not None:
            try:
                self.on_slot_removed(slot_key)
            except Exception:
                pass
        for sid, mapped in list(self._session_affinity.items()):
            if mapped == slot_key:
                self._session_affinity.pop(sid, None)
        for req_id, inflight in list(self._pending.items()):
            if inflight.slot_key == slot_key:
                inflight.events.put_nowait(
                    {
                        "type": "error",
                        "message": f"执行该会话的 Gemini Enterprise 标签页已关闭 ({reason})。",
                    }
                )
                self._pending.pop(req_id, None)

    def _sync_client_tabs(self, client_id: str, tabs_payload: List[Dict[str, Any]]) -> None:
        state = self._clients.get(client_id)
        seen_keys: Set[str] = set()

        # The extension uses this same tab order for the numbers shown in Chrome.
        for seat_index, item in enumerate(tabs_payload, start=1):
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("available_models"), list):
                self.record_discovered_models(item.get("available_models"))
            raw_tid = item.get("tab_id") if item.get("tab_id") is not None else item.get("id")
            tid = int(raw_tid) if raw_tid is not None else None
            slot = self._upsert_single_slot(
                client_id=client_id,
                tab_id=tid,
                tab_url=str(item.get("tab_url") or item.get("url") or ""),
                tab_title=str(item.get("tab_title") or item.get("title") or ""),
                tab_ready=bool(item.get("ready", True)),
                active_model=str(item.get("active_model") or ""),
                captured_rpc=item.get("captured_rpc") if "captured_rpc" in item else None,
            )
            slot.seat_index = seat_index
            seen_keys.add(slot.slot_key)

        # Prune slots belonging to `client_id` that were closed in the browser,
        # while protecting actively working/queued slots and freshly spawned tabs from stale snapshots.
        now_sync = time.time()
        for existing_key, existing_slot in list(self._slots.items()):
            if existing_slot.client_id == client_id and existing_key not in seen_keys:
                if (
                    existing_slot.is_busy
                    or existing_slot.waiting_count > 0
                    or (
                        state is not None
                        and state.spawning_count > 0
                        and (now_sync - existing_slot.created_at) < 30.0
                    )
                ):
                    continue
                self._remove_slot(existing_key, reason="tab_removed")

        if state is not None:
            client_slots = [
                s for s in self._slots.values() if s.client_id == client_id and s.tab_ready
            ]
            if client_slots:
                primary = client_slots[0]
                state.tab_id = primary.tab_id
                state.tab_url = primary.tab_url
                state.tab_title = primary.tab_title
                state.tab_ready = True
                if primary.active_model:
                    state.active_model = primary.active_model
                state.captured_rpc = any(s.captured_rpc for s in client_slots)
            else:
                state.tab_id = None
                state.tab_url = ""
                state.tab_title = ""
                state.tab_ready = False

    def _get_alive_ready_slots(self) -> List[WorkerSlot]:
        self._prune_stale_http_clients()
        now = time.time()
        ready_slots: List[WorkerSlot] = []
        for slot in list(self._slots.values()):
            client = self._clients.get(slot.client_id)
            if client is not None and client.is_alive(now) and slot.tab_ready:
                ready_slots.append(slot)
        ready_slots.sort(
            key=lambda s: (
                1 if "vertexaisearch.cloud.google.com" in (s.tab_url or "") else 0,
                1 if self._clients[s.client_id].transport == "websocket" else 0,
                -s.waiting_count,
            ),
            reverse=True,
        )
        return ready_slots

    def list_ready_slot_keys(self) -> List[str]:
        return [s.slot_key for s in self._get_alive_ready_slots()]

    def ready_slot_url_map(self) -> Dict[str, str]:
        return {s.slot_key: (s.tab_url or "") for s in self._get_alive_ready_slots()}

    def hydratable_slot_url_map(self) -> Dict[str, str]:
        return {
            s.slot_key: (s.tab_url or "")
            for s in self._get_alive_ready_slots()
            if not s.is_busy
            and s.waiting_count == 0
            and not s.lock.locked()
            and s.completed_requests == 0
            and s.last_used_at == 0.0
            and not s.pinned_session_id
        }

    # ------------------------------------------------------------------
    # Client registration & lifecycle
    # ------------------------------------------------------------------

    def _get_ws_usage_snapshot(self, max_age: float = 3.0) -> Optional[Dict[str, Any]]:
        if self.usage_snapshot_provider is None:
            return None
        now_mono = time.monotonic()
        if (
            self._cached_ws_usage_snapshot is not None
            and (now_mono - self._cached_ws_usage_at) <= max_age
        ):
            return self._cached_ws_usage_snapshot
        try:
            snap = self.usage_snapshot_provider()
            self._cached_ws_usage_snapshot = snap
            self._cached_ws_usage_at = now_mono
            return snap
        except Exception:
            return self._cached_ws_usage_snapshot

    def _abort_client_pending_requests(self, client_id: str, reason: str) -> None:
        for req_id, inflight in list(self._pending.items()):
            if inflight.client_id == client_id:
                inflight.events.put_nowait(
                    {
                        "type": "error",
                        "message": f"浏览器桥接连接在生成过程中断开 ({reason})，请确认页面未关闭。",
                    }
                )
                self._pending.pop(req_id, None)

    def _schedule_ws_grace_watchdog(self, client_id: str, reason: str) -> None:
        prev_task = self._ws_grace_tasks.pop(client_id, None)
        if prev_task is not None and not prev_task.done():
            prev_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._abort_client_pending_requests(client_id, reason)
            return

        async def _watchdog() -> None:
            try:
                await asyncio.sleep(self.ws_reconnect_grace_seconds)
                st = self._clients.get(client_id)
                if st is None or (st.transport == "websocket" and st.websocket is None):
                    self._abort_client_pending_requests(client_id, reason)
            except asyncio.CancelledError:
                return
            finally:
                if self._ws_grace_tasks.get(client_id) is task:
                    self._ws_grace_tasks.pop(client_id, None)

        task = loop.create_task(_watchdog())
        self._ws_grace_tasks[client_id] = task

    async def register_websocket(self, websocket: WebSocket, client_id: Optional[str] = None) -> BrowserClientState:
        cid = client_id or f"ws-{uuid.uuid4().hex[:8]}"
        prev_grace = self._ws_grace_tasks.pop(cid, None)
        if prev_grace is not None and not prev_grace.done():
            prev_grace.cancel()
        prev = self._clients.get(cid)
        queued_cancels = list(prev.pending_cancels) if prev is not None else []
        state = BrowserClientState(
            client_id=cid,
            transport="websocket",
            websocket=websocket,
            tab_id=prev.tab_id if prev else None,
            tab_url=prev.tab_url if prev else "",
            tab_title=prev.tab_title if prev else "",
            tab_ready=prev.tab_ready if prev else True,
            driver_mode=prev.driver_mode if prev else "auto",
            captured_rpc=prev.captured_rpc if prev else False,
            active_model=prev.active_model if prev else "",
            extension_version=prev.extension_version if prev else "",
            capabilities=set(prev.capabilities) if prev else set(),
            client_max_parallel_tabs=prev.client_max_parallel_tabs if prev else None,
            spawning_count=prev.spawning_count if prev else 0,
            disconnected_at=0.0,
        )
        self._clients[cid] = state
        if prev is not None and prev.websocket is not None and prev.websocket is not websocket:
            old_ws = prev.websocket
            prev.websocket = None
            try:
                await old_ws.close(code=1000)
            except Exception:
                pass
        self._ws_reconnect_event.set()
        self._parallel_limit.wake_waiters()
        self.add_log("info", f"Extension WebSocket connected ({cid})", cid)
        welcome_payload: Dict[str, Any] = {
            "type": "welcome",
            "client_id": cid,
            "server_time": int(time.time()),
            "backend_version": self.backend_version,
            "max_parallel_tabs": self.max_parallel_tabs,
            "auto_spawn_tabs": self.auto_spawn_tabs,
            "busy_tab_ids": self.compute_working_tab_ids(),
        }
        usage_snap = self._get_ws_usage_snapshot(max_age=3.0)
        if usage_snap is not None:
            welcome_payload["usage_snapshot"] = usage_snap
        await self._safe_ws_send(state, welcome_payload)
        for cancel_req_id, cancel_tab_id, force_clear in queued_cancels:
            cancel_payload: Dict[str, Any] = {"type": "cancel_request", "request_id": cancel_req_id}
            if cancel_tab_id is not None:
                cancel_payload["tab_id"] = cancel_tab_id
            if force_clear:
                cancel_payload["force_tab_clear"] = True
            await self._safe_ws_send(state, cancel_payload)
        return state

    async def broadcast_ws_event(
        self,
        payload: Dict[str, Any],
        required_capability: Optional[str] = "usage_push",
    ) -> None:
        now = time.time()
        targets = [
            c
            for c in list(self._clients.values())
            if c.transport == "websocket"
            and c.is_alive(now)
            and (not required_capability or required_capability in c.capabilities)
        ]
        for client in targets:
            await self._safe_ws_send(client, payload)

    def unregister_client(
        self,
        client_id: str,
        reason: str = "disconnected",
        websocket: Optional[WebSocket] = None,
        drop_slots: Optional[bool] = None,
    ) -> None:
        existing = self._clients.get(client_id)
        if existing is None:
            return
        if websocket is not None and existing.transport == "websocket" and existing.websocket is not websocket:
            return
        should_drop_slots = drop_slots if drop_slots is not None else (existing.transport != "websocket")
        if should_drop_slots:
            prev_grace = self._ws_grace_tasks.pop(client_id, None)
            if prev_grace is not None and not prev_grace.done():
                prev_grace.cancel()
            state = self._clients.pop(client_id, None)
            if state is not None:
                state.websocket = None
                self.add_log("warn", f"Browser client {client_id} disconnected ({reason})", client_id)
            for skey, slot in list(self._slots.items()):
                if slot.client_id == client_id:
                    self._remove_slot(skey, reason=reason)
            self._abort_client_pending_requests(client_id, reason)
            return

        # Keep WS client, slot conversation ledgers, and active in-flight generations alive across
        # MV3 Service Worker sleep / transient WebSocket reconnects.
        now = time.time()
        existing.websocket = None
        existing.last_seen_at = now
        existing.disconnected_at = now
        inflight_cnt = sum(1 for inf in self._pending.values() if inf.client_id == client_id)
        if inflight_cnt > 0 and self.ws_reconnect_grace_seconds > 0:
            self.add_log(
                "warn",
                f"Browser client {client_id} socket closed ({reason}); keeping {inflight_cnt} in-flight request(s) alive for {self.ws_reconnect_grace_seconds:.0f}s reconnect grace",
                client_id,
            )
            self._schedule_ws_grace_watchdog(client_id, reason)
        elif inflight_cnt > 0:
            self._abort_client_pending_requests(client_id, reason)
        else:
            self.add_log("warn", f"Browser client {client_id} socket closed ({reason}); preserving slot affinity", client_id)

    def register_http_client(
        self,
        client_id: Optional[str] = None,
        user_agent: str = "",
        tab_id: Optional[int] = None,
        tab_url: str = "",
        tab_title: str = "",
        driver_mode: str = "auto",
        active_model: str = "",
        available_models: Optional[Sequence[str]] = None,
    ) -> BrowserClientState:
        self._prune_stale_http_clients()
        self.record_discovered_models(available_models, active_model=active_model)
        cid = client_id or f"tm-{uuid.uuid4().hex[:8]}"
        existing = self._clients.get(cid)
        if existing and existing.transport == "http_poll":
            existing.last_seen_at = time.time()
            existing.user_agent = user_agent or existing.user_agent
            if tab_id is not None:
                existing.tab_id = tab_id
            existing.tab_url = tab_url or existing.tab_url
            existing.tab_title = tab_title or existing.tab_title
            existing.driver_mode = driver_mode or existing.driver_mode
            if active_model:
                existing.active_model = active_model
            existing.tab_ready = True
            self._upsert_single_slot(
                client_id=cid,
                tab_id=existing.tab_id,
                tab_url=existing.tab_url,
                tab_title=existing.tab_title,
                tab_ready=True,
                active_model=existing.active_model,
            )
            return existing

        state = BrowserClientState(
            client_id=cid,
            transport="http_poll",
            user_agent=user_agent,
            tab_id=tab_id,
            tab_url=tab_url,
            tab_title=tab_title,
            tab_ready=True,
            driver_mode=driver_mode or "auto",
            active_model=active_model,
        )
        self._clients[cid] = state
        self._upsert_single_slot(
            client_id=cid,
            tab_id=tab_id,
            tab_url=tab_url,
            tab_title=tab_title,
            tab_ready=True,
            active_model=active_model,
        )
        self.add_log("info", f"Tampermonkey HTTP client connected ({cid})", cid)
        return state

    async def poll_http_command(self, client_id: str, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        state = self._clients.get(client_id)
        if state is None:
            state = self.register_http_client(client_id=client_id)
        state.last_seen_at = time.time()
        try:
            return await asyncio.wait_for(state.command_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            state.last_seen_at = time.time()
            return None

    def _prune_stale_http_clients(self) -> None:
        now = time.time()
        for cid, state in list(self._clients.items()):
            if state.transport == "http_poll" and not state.is_alive(now):
                self.unregister_client(cid, reason="http_poll_timeout")
            elif state.transport == "websocket" and state.websocket is None and (now - state.last_seen_at) > 86400.0:
                self.unregister_client(cid, reason="ws_dormant_expired", drop_slots=True)

    def select_active_client(self) -> Optional[BrowserClientState]:
        """Return the primary healthy connected browser client."""
        self._prune_stale_http_clients()
        now = time.time()
        alive = [c for c in self._clients.values() if c.is_alive(now) and c.tab_ready]
        if not alive:
            return None

        def sort_key(c: BrowserClientState) -> tuple:
            has_vertex_tab = "vertexaisearch.cloud.google.com" in (c.tab_url or "")
            return (
                1 if has_vertex_tab else 0,
                1 if c.transport == "websocket" else 0,
                c.last_seen_at,
            )

        alive.sort(key=sort_key, reverse=True)
        return alive[0]

    def _has_spawnable_ws_client(self, now: Optional[float] = None) -> bool:
        if not self.auto_spawn_tabs:
            return False
        ts = time.time() if now is None else now
        return any(
            c.transport == "websocket" and c.is_alive(ts) and "spawn_tab" in c.capabilities
            for c in self._clients.values()
        )

    def has_ready_client(self) -> bool:
        return (
            len(self._get_alive_ready_slots()) > 0
            or self.select_active_client() is not None
            or self._has_spawnable_ws_client()
        )

    def has_reconnecting_ws_client(self, now: Optional[float] = None) -> bool:
        if self.ws_reconnect_grace_seconds <= 0:
            return False
        ts = time.time() if now is None else now
        for c in self._clients.values():
            if c.transport != "websocket":
                continue
            if (
                c.websocket is None
                and c.disconnected_at > 0.0
                and (ts - c.disconnected_at) <= self.ws_reconnect_grace_seconds
            ):
                return True
            if (
                c.websocket is not None
                and not c.tab_ready
                and (ts - c.connected_at) <= 5.0
            ):
                return True
        return False

    async def wait_for_client_reconnect(
        self,
        client_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        budget = min(
            self.ws_reconnect_wait_seconds if timeout is None else max(0.0, float(timeout)),
            self.ws_reconnect_grace_seconds,
        )
        if budget <= 0:
            return False
        deadline = time.monotonic() + budget
        while True:
            now = time.time()
            if client_id:
                st = self._clients.get(client_id)
                if st is not None and st.is_alive(now):
                    return True
                if (
                    st is None
                    or st.transport != "websocket"
                    or st.disconnected_at <= 0.0
                    or (now - st.disconnected_at) > self.ws_reconnect_grace_seconds
                ):
                    return False
            else:
                if self.has_ready_client():
                    return True
                if not self.has_reconnecting_ws_client(now):
                    return False
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            self._ws_reconnect_event.clear()
            if client_id:
                st_recheck = self._clients.get(client_id)
                if st_recheck is not None and st_recheck.is_alive():
                    return True
            elif self.has_ready_client():
                return True
            try:
                await asyncio.wait_for(self._ws_reconnect_event.wait(), timeout=min(rem, 0.25))
            except asyncio.TimeoutError:
                continue

    async def wait_for_ready_client(self, timeout: Optional[float] = None) -> bool:
        if self.has_ready_client():
            return True
        return await self.wait_for_client_reconnect(client_id=None, timeout=timeout)

    def effective_pool_capacity(self) -> int:
        """Return max parallel browser slots available right now (accounting for auto-spawn when supported)."""
        slots = self._get_alive_ready_slots()
        active = self.select_active_client()
        now = time.time()
        can_spawn = self._has_spawnable_ws_client(now)
        if not slots and active is None:
            return self.max_parallel_tabs if can_spawn else 0
        eff_max = self._effective_client_tab_cap(active) if active is not None else self.max_parallel_tabs
        live_count = len(slots) if slots else (1 if active is not None else 0)
        if can_spawn:
            return max(min(len(slots), eff_max), eff_max)
        return min(live_count, eff_max)

    def status_snapshot(self) -> Dict[str, Any]:
        self._prune_stale_http_clients()
        now = time.time()
        alive_clients = [c for c in self._clients.values() if c.is_alive(now)]
        clients = [c.to_public_dict() for c in alive_clients]
        slots = self._get_alive_ready_slots()
        active = self.select_active_client()
        reported_client_cap = (
            active.client_max_parallel_tabs
            if active is not None
            else next(
                (c.client_max_parallel_tabs for c in alive_clients if c.client_max_parallel_tabs is not None),
                None,
            )
        )
        effective_max_tabs = (
            self._effective_client_tab_cap(active)
            if active is not None
            else self._effective_global_tab_cap()
        )
        parallel_cap = min(len(slots), effective_max_tabs)
        busy_slots = sum(1 for s in slots if s.is_busy)
        waiting_total = sum(s.waiting_count for s in slots)
        connected_ext_ver = ""
        for st in alive_clients:
            if st.extension_version and (
                not connected_ext_ver
                or compare_semver(st.extension_version, connected_ext_ver) > 0
            ):
                connected_ext_ver = st.extension_version
        backend_update_required = bool(
            connected_ext_ver and compare_semver(connected_ext_ver, self.backend_version) > 0
        )
        return {
            "browser_connected": len(slots) > 0 or active is not None,
            "backend_version": self.backend_version,
            "connected_extension_version": connected_ext_ver,
            "backend_update_required": backend_update_required,
            "active_client": active.to_public_dict() if active else None,
            "clients": clients,
            "worker_slots": [
                {**s.to_public_dict(), "codex_activity_owner": not self._is_duplicate_session_slot(s, now)}
                for s in slots
            ],
            "parallel_capacity": parallel_cap,
            "connected_tabs": len(slots),
            "max_parallel_tabs": effective_max_tabs,
            "unlimited_parallel_tabs": False,
            "client_max_parallel_tabs": reported_client_cap,
            "server_max_parallel_tabs": self.max_parallel_tabs,
            "auto_spawn_tabs": self.auto_spawn_tabs,
            "active_slots_busy": busy_slots,
            "busy": len(self._pending) >= parallel_cap > 0,
            "queued_requests": waiting_total,
            "in_flight_requests": len(self._pending),
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "discovered_models": list(self._discovered_web_models),
            "recent_logs": self._recent_logs[-25:],
        }

    # ------------------------------------------------------------------
    # Incoming browser messages (WebSocket & HTTP bridge unified handler)
    # ------------------------------------------------------------------

    async def handle_browser_event(self, client_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        state = self._clients.get(client_id)
        if state is not None:
            state.last_seen_at = time.time()
        if isinstance(data.get("available_models"), list) or data.get("active_model"):
            self.record_discovered_models(
                data.get("available_models") if isinstance(data.get("available_models"), list) else None,
                active_model=str(data.get("active_model") or ""),
            )

        msg_type = str(data.get("type") or data.get("event") or "").strip()

        if msg_type == "spawn_tab_result":
            spawn_id = str(data.get("spawn_id") or "")
            fut = self._pending_spawns.get(spawn_id)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return None

        if msg_type == "reset_tab_result":
            reset_id = str(data.get("reset_id") or "")
            fut = self._pending_resets.get(reset_id)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return None

        if msg_type == "open_session_result":
            open_id = str(data.get("open_id") or "")
            fut = self._pending_opens.get(open_id)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return None

        if msg_type in {"reload_extension_ack", "set_max_parallel_tabs_ack"}:
            return None

        if msg_type in {"hello", "tab_status", "heartbeat"}:
            reported_ext_ver = normalize_version(str(data.get("extension_version") or ""))
            emit_ext_reload = False
            backend_outdated_flag = False
            if state is not None:
                if isinstance(data.get("capabilities"), list):
                    state.capabilities = {str(x) for x in data["capabilities"] if x}
                if reported_ext_ver:
                    state.extension_version = reported_ext_ver
                    if self.on_extension_version_reported is not None:
                        try:
                            self.on_extension_version_reported(reported_ext_ver)
                        except Exception:
                            pass
                    cmp_ver = compare_semver(reported_ext_ver, self.backend_version)
                    if cmp_ver > 0:
                        backend_outdated_flag = True
                        if state.warned_outdated_backend_for != reported_ext_ver:
                            state.warned_outdated_backend_for = reported_ext_ver
                            self.add_log(
                                "warn",
                                f"浏览器插件已升级至 v{reported_ext_ver}（当前后端 v{self.backend_version}）— 请更新后端",
                                client_id,
                            )
                    elif cmp_ver < 0:
                        if self.on_outdated_extension_detected is not None:
                            try:
                                self.on_outdated_extension_detected(reported_ext_ver)
                            except Exception:
                                pass
                        target_combo = f"{reported_ext_ver}->{self.backend_version}"
                        if state.notified_reload_for_version != target_combo:
                            state.notified_reload_for_version = target_combo
                            emit_ext_reload = True
                    else:
                        reported_driver_ver = normalize_version(str(data.get("driver_version") or ""))
                        if (
                            reported_driver_ver
                            and self.expected_driver_version
                            and compare_semver(reported_driver_ver, self.expected_driver_version) < 0
                        ):
                            target_combo = f"{reported_ext_ver}:{reported_driver_ver}->{self.backend_version}:{self.expected_driver_version}"
                            if state.notified_reload_for_version != target_combo:
                                state.notified_reload_for_version = target_combo
                                emit_ext_reload = True
                if data.get("max_parallel_tabs") is not None:
                    try:
                        raw_cap = int(data["max_parallel_tabs"])
                        state.client_max_parallel_tabs = (
                            self.max_parallel_tabs if raw_cap <= 0 else min(200, raw_cap)
                        )
                        self._parallel_limit.wake_waiters()
                    except (TypeError, ValueError):
                        pass
                if data.get("driver_mode"):
                    state.driver_mode = str(data.get("driver_mode"))
                if data.get("user_agent"):
                    state.user_agent = str(data.get("user_agent"))

                if isinstance(data.get("tabs"), list):
                    self._sync_client_tabs(client_id, data["tabs"])
                else:
                    if "tab_id" in data and data.get("tab_id") is not None:
                        state.tab_id = int(data["tab_id"])
                    if data.get("tab_url") is not None:
                        state.tab_url = str(data.get("tab_url") or "")
                    if data.get("tab_title") is not None:
                        state.tab_title = str(data.get("tab_title") or "")
                    if data.get("ready") is not None:
                        state.tab_ready = bool(data.get("ready"))
                    if data.get("captured_rpc") is not None:
                        state.captured_rpc = bool(data.get("captured_rpc"))
                    if data.get("active_model"):
                        state.active_model = str(data.get("active_model"))

                    if state.tab_ready:
                        self._upsert_single_slot(
                            client_id=client_id,
                            tab_id=state.tab_id,
                            tab_url=state.tab_url,
                            tab_title=state.tab_title,
                            tab_ready=True,
                            active_model=state.active_model,
                            captured_rpc=state.captured_rpc,
                        )
                    else:
                        self._sync_client_tabs(client_id, [])

            self._ws_reconnect_event.set()
            resp: Dict[str, Any] = {
                "type": "pong" if msg_type == "heartbeat" else "ack",
                "server_time": int(time.time()),
                "backend_version": self.backend_version,
                "active_request_ids": [
                    rid for rid, inf in self._pending.items() if inf.client_id == client_id
                ],
            }
            if backend_outdated_flag:
                resp["backend_update_required"] = True
            if emit_ext_reload:
                resp["_emit_extension_reload"] = self.backend_version
            if state is not None and "session_busy_sync" in state.capabilities:
                resp["busy_tab_ids"] = self.compute_working_tab_ids()
            usage_snap = self._get_ws_usage_snapshot(max_age=3.0)
            if usage_snap is not None:
                resp["usage_snapshot"] = usage_snap
            return resp

        if msg_type == "log":
            self.add_log(
                str(data.get("level") or "info"),
                str(data.get("message") or ""),
                client_id=client_id,
            )
            return None

        req_id = str(data.get("request_id") or "").strip()
        if not req_id:
            return None

        inflight = self._pending.get(req_id)
        if inflight is None:
            return None

        if inflight.client_id != client_id:
            return None
        if data.get("tab_id") is not None:
            try:
                if int(data["tab_id"]) != inflight.tab_id:
                    return None
            except (TypeError, ValueError):
                return None

        inflight.last_activity_at = time.time()
        slot_obj = self._slots.get(inflight.slot_key)

        if msg_type == "request_ack":
            return None

        if msg_type == "request_progress":
            dom_len = int(data.get("dom_len") or 0)
            net_len = int(data.get("net_len") or 0)
            if data.get("tab_reset"):
                inflight.tab_reset = True
                if slot_obj is not None:
                    slot_obj.last_dispatch_tab_reset = True
            if data.get("submitted") is True or dom_len > 0 or net_len > 0:
                inflight.submitted = True
                if slot_obj is not None:
                    slot_obj.last_dispatch_submitted = True
            await inflight.events.put(
                {
                    "type": "progress",
                    "generating": bool(data.get("generating", True)),
                    "dom_len": dom_len,
                    "net_len": net_len,
                }
            )
            return None

        if msg_type == "stream_chunk":
            inflight.submitted = True
            if slot_obj is not None:
                slot_obj.last_dispatch_submitted = True
            delta = str(data.get("delta") or "")
            if delta:
                await inflight.events.put({"type": "chunk", "delta": delta})
            return None

        if msg_type == "reply_chunk":
            inflight.submitted = True
            if slot_obj is not None:
                slot_obj.last_dispatch_submitted = True
            c_idx = int(data.get("chunk_index") or 0)
            c_total = int(data.get("total_chunks") or 0)
            c_data = str(data.get("data") or "")
            inflight.reply_chunks[c_idx] = c_data
            if c_total > 0:
                inflight.expected_reply_chunks = c_total
            return None

        if msg_type == "request_error":
            if data.get("tab_reset"):
                inflight.tab_reset = True
                if slot_obj is not None:
                    slot_obj.last_dispatch_tab_reset = True
            err_msg = str(data.get("error") or data.get("message") or "浏览器页面执行失败")
            if data.get("submitted") is False and not inflight.submitted:
                if slot_obj is not None and "仍在处理上一项请求" in err_msg:
                    slot_obj.tab_busy_until = time.time() + 12.0
            else:
                inflight.submitted = True
                if slot_obj is not None:
                    slot_obj.last_dispatch_submitted = True
            self._total_errors += 1
            self.add_log("error", f"Request {req_id[:8]} error: {err_msg}", client_id)
            await inflight.events.put({"type": "error", "message": err_msg})
            return None

        if msg_type == "request_complete" or "reply" in data:
            inflight.submitted = True
            if slot_obj is not None:
                slot_obj.last_dispatch_submitted = True
            if data.get("chunked"):
                total_c = int(
                    data.get("total_chunks")
                    or inflight.expected_reply_chunks
                    or len(inflight.reply_chunks)
                )
                if total_c <= 0 or len(inflight.reply_chunks) != total_c or any(i not in inflight.reply_chunks for i in range(total_c)):
                    self._total_errors += 1
                    await inflight.events.put({"type": "error", "message": "网页回复分段缺失，未将不完整内容标为完成。"})
                    return None
                reply = "".join(inflight.reply_chunks[i] for i in range(total_c))
                inflight.reply_chunks.clear()
            else:
                reply = str(data.get("reply") or "")
            citations = data.get("citations") if isinstance(data.get("citations"), list) else []
            source = str(data.get("source") or "browser")

            slot = self._slots.get(inflight.slot_key)
            if slot is not None:
                slot.completed_requests += 1
                if data.get("tab_url"):
                    slot.tab_url = str(data["tab_url"])
                if data.get("tab_title"):
                    slot.tab_title = str(data["tab_title"])
                if data.get("active_model"):
                    slot.active_model = str(data["active_model"])
            if state is not None:
                state.completed_requests += 1
                if data.get("tab_url") and (state.tab_id is None or state.tab_id == inflight.tab_id):
                    state.tab_url = str(data["tab_url"])
                if data.get("tab_title") and (state.tab_id is None or state.tab_id == inflight.tab_id):
                    state.tab_title = str(data["tab_title"])
                if data.get("active_model"):
                    state.active_model = str(data["active_model"])

            await inflight.events.put(
                {
                    "type": "done",
                    "reply": reply,
                    "output_images": data.get("output_images") or [],
                    "output_files": data.get("output_files") or [],
                    "citations": citations,
                    "source": source,
                }
            )
            return None

        return None

    # ------------------------------------------------------------------
    # Multi-Tab Scheduler, Auto-Spawning & Atomic Slot Leasing
    # ------------------------------------------------------------------

    def _effective_client_tab_cap(self, client: BrowserClientState) -> int:
        if client.client_max_parallel_tabs is not None and client.client_max_parallel_tabs > 0:
            return min(200, client.client_max_parallel_tabs)
        return self.max_parallel_tabs

    def _effective_global_tab_cap(self) -> int:
        now = time.time()
        explicit_clients = [
            c for c in self._clients.values() if c.is_alive(now) and c.client_max_parallel_tabs is not None
        ]
        if explicit_clients:
            return sum(self._effective_client_tab_cap(c) for c in explicit_clients)
        active = self.select_active_client()
        if active is not None:
            return self._effective_client_tab_cap(active)
        return self.max_parallel_tabs

    def _find_spawnable_client(self, ready_slots: Sequence[WorkerSlot]) -> Optional[tuple[BrowserClientState, str]]:
        """Find a connected WS extension client capable of spawning another background Vertex tab."""
        if not self.auto_spawn_tabs:
            return None
        global_cap = self._effective_global_tab_cap()
        if len(ready_slots) + sum(c.spawning_count for c in self._clients.values()) >= global_cap:
            return None
        now = time.time()
        for client in self._clients.values():
            if (
                client.transport != "websocket"
                or not client.is_alive(now)
                or "spawn_tab" not in client.capabilities
            ):
                continue
            client_slots = [s for s in ready_slots if s.client_id == client.client_id]
            cap = self._effective_client_tab_cap(client)
            if len(client_slots) + client.spawning_count < cap:
                seed_url = ""
                for s in client_slots:
                    if "vertexaisearch.cloud.google.com" in (s.tab_url or ""):
                        seed_url = derive_clean_vertex_url(s.tab_url)
                        break
                if not seed_url and client.tab_url:
                    seed_url = derive_clean_vertex_url(client.tab_url)
                return (client, seed_url or "https://vertexaisearch.cloud.google.com/")
        return None

    async def _spawn_worker_tab(
        self,
        client: BrowserClientState,
        base_url: str,
        preserve_session_url: bool = False,
    ) -> Optional[WorkerSlot]:
        spawn_id = f"sp-{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_spawns[spawn_id] = fut

        self.add_log("info", f"Auto-spawning parallel worker tab ({spawn_id}) ...", client.client_id)
        payload: Dict[str, Any] = {
            "type": "spawn_tab",
            "spawn_id": spawn_id,
            "base_url": base_url,
        }
        if preserve_session_url:
            payload["preserve_session_url"] = True
        try:
            if not await self._safe_ws_send(client, payload):
                return None
            res = await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self.add_log("warn", f"Auto-spawn tab timed out ({spawn_id}); falling back to existing pool", client.client_id)
            return None
        finally:
            self._pending_spawns.pop(spawn_id, None)

        if not isinstance(res, dict) or not res.get("ok"):
            return None
        tab_info = res.get("tab")
        if not isinstance(tab_info, dict):
            return None
        raw_tid = tab_info.get("tab_id") if tab_info.get("tab_id") is not None else tab_info.get("id")
        if raw_tid is None:
            return None

        slot = self._upsert_single_slot(
            client_id=client.client_id,
            tab_id=int(raw_tid),
            tab_url=str(tab_info.get("tab_url") or tab_info.get("url") or base_url),
            tab_title=str(tab_info.get("tab_title") or tab_info.get("title") or "Gemini Enterprise"),
            tab_ready=True,
            active_model=str(tab_info.get("active_model") or ""),
        )
        self.add_log("info", f"Parallel worker tab #{slot.tab_id} ready ({slot.slot_key})", client.client_id)
        return slot

    def _is_duplicate_session_slot(self, slot: WorkerSlot, now: float) -> bool:
        sid = slot.pinned_session_id
        mapped_key = self._session_affinity.get(sid) if sid else None
        if not mapped_key or mapped_key == slot.slot_key:
            return False
        owner = self._slots.get(mapped_key)
        if owner is None or owner.pinned_session_id != sid or not owner.tab_ready:
            return False
        client = self._clients.get(owner.client_id)
        return client is not None and client.is_alive(now)

    def _is_slot_codex_active(self, slot: WorkerSlot, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else now
        if self._is_duplicate_session_slot(slot, ts):
            return ts < slot.pending_tool_call_until
        if self.is_slot_session_active is None:
            return False
        try:
            return bool(self.is_slot_session_active(slot))
        except Exception:
            return False

    def compute_working_tab_ids(self, now: Optional[float] = None) -> List[int]:
        ts = time.time() if now is None else now
        working_ids: Set[int] = set()
        for s in self._get_alive_ready_slots():
            if s.tab_id is None:
                continue
            if s.is_busy or s.waiting_count > 0 or ts < s.pending_tool_call_until or self._is_slot_codex_active(s, ts):
                working_ids.add(int(s.tab_id))
        return sorted(working_ids)

    async def push_working_tabs_if_changed(self, tab_ids: Optional[Sequence[int]] = None) -> None:
        ids = sorted({int(tid) for tid in (tab_ids if tab_ids is not None else self.compute_working_tab_ids()) if tid is not None})
        frozen = frozenset(ids)
        if getattr(self, "_last_pushed_busy_tabs", None) == frozen:
            return
        self._last_pushed_busy_tabs = frozen
        now = time.time()
        payload = {"type": "sync_busy_tabs", "busy_tab_ids": ids}
        for client in list(self._clients.values()):
            if (
                client.transport == "websocket"
                and client.websocket is not None
                and "session_busy_sync" in client.capabilities
                and client.is_alive(now)
            ):
                await self._safe_ws_send(client, payload)

    async def reset_worker_slot(
        self,
        slot_key: str,
        reason: Optional[str] = None,
        on_reset_locked: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Reset a single idle worker slot to a fresh conversation without disturbing busy slots."""
        slot = self._slots.get(slot_key)
        if slot is None:
            raise KeyError("未找到指定的工作席位")
        if slot.is_busy or slot.waiting_count > 0 or self._is_slot_codex_active(slot):
            raise RuntimeError("该工作席位正在执行任务中，请等待本轮任务完成后再开启新对话")

        async with slot.lock:
            if slot.waiting_count > 0 or slot.in_flight_request_id is not None:
                raise RuntimeError("该工作席位正在执行任务中，请等待本轮任务完成后再开启新对话")
            slot.pinned_session_id = None
            slot.owner_tenant_id = None
            slot.pending_tool_call_until = 0.0
            slot.last_used_at = 0.0
            slot.tab_title = "Gemini Enterprise"
            if slot.tab_url:
                slot.tab_url = derive_clean_vertex_url(slot.tab_url)
            for sid, mapped in list(self._session_affinity.items()):
                if mapped == slot_key:
                    self._session_affinity.pop(sid, None)

            in_tab_reset = False
            client = self._clients.get(slot.client_id)
            now = time.time()
            if (
                client is not None
                and client.transport == "websocket"
                and client.is_alive(now)
                and slot.tab_id is not None
                and "reset_tab" in client.capabilities
            ):
                reset_id = f"rst-{uuid.uuid4().hex[:10]}"
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
                self._pending_resets[reset_id] = fut
                sent = await self._safe_ws_send(
                    client,
                    {
                        "type": "reset_tab",
                        "reset_id": reset_id,
                        "tab_id": slot.tab_id,
                    },
                )
                if sent:
                    try:
                        res = await asyncio.wait_for(fut, timeout=14.0)
                        in_tab_reset = bool(isinstance(res, dict) and res.get("ok"))
                    except asyncio.TimeoutError:
                        in_tab_reset = False
                    finally:
                        self._pending_resets.pop(reset_id, None)
                else:
                    self._pending_resets.pop(reset_id, None)

            slot.tab_title = "Gemini Enterprise"
            if slot.tab_url:
                slot.tab_url = derive_clean_vertex_url(slot.tab_url)
            if on_reset_locked is not None:
                on_reset_locked()

        label = f"#{slot.tab_id}" if slot.tab_id is not None else slot_key
        self.add_log("info", reason or f"工作席位 {label} 已重置为新对话", slot.client_id)
        return {
            "ok": True,
            "slot_key": slot_key,
            "tab_id": slot.tab_id,
            "in_tab_reset": in_tab_reset,
        }

    async def navigate_slot_to_session(
        self,
        slot: WorkerSlot,
        session_url: str,
        timeout: float = 18.0,
    ) -> bool:
        """Navigate an acquired worker slot directly to an archived `/r/session/<id>` URL and verify it loaded."""
        target_vsid = extract_vertex_session_id(session_url)
        if not target_vsid:
            return False
        if extract_vertex_session_id(slot.tab_url) == target_vsid:
            return True

        client = self._clients.get(slot.client_id)
        now = time.time()
        if (
            client is None
            or client.transport != "websocket"
            or not client.is_alive(now)
            or slot.tab_id is None
            or "open_session" not in client.capabilities
        ):
            return False

        open_id = f"op-{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_opens[open_id] = fut
        sent = await self._safe_ws_send(
            client,
            {
                "type": "open_session",
                "open_id": open_id,
                "tab_id": slot.tab_id,
                "session_url": session_url,
            },
        )
        if not sent:
            self._pending_opens.pop(open_id, None)
            return False

        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.add_log(
                "warn",
                f"切回历史会话超时 ({target_vsid[:12]})，将回退开启新会话",
                slot.client_id,
            )
            return False
        finally:
            self._pending_opens.pop(open_id, None)

        if not isinstance(res, dict) or not res.get("ok"):
            return False
        final_url = str(res.get("tab_url") or session_url)
        if extract_vertex_session_id(final_url) != target_vsid:
            return False
        slot.tab_url = final_url
        if res.get("tab_title"):
            slot.tab_title = str(res["tab_title"])
        if res.get("active_model"):
            slot.active_model = str(res["active_model"])
        label = f"#{slot.tab_id}" if slot.tab_id is not None else slot.slot_key
        self.add_log("info", f"工作席位 {label} 已重入历史会话 ({target_vsid[:12]})", slot.client_id)
        return True

    async def reload_connected_extensions(self) -> Dict[str, Any]:
        if self._pending:
            raise RuntimeError("当前仍有请求正在执行，请等待空闲后再重载扩展。")
        reloaded: List[str] = []
        for cid, client in list(self._clients.items()):
            if client.transport == "websocket" and client.websocket is not None:
                if await self._safe_ws_send(client, {"type": "reload_extension"}):
                    reloaded.append(cid)
        return {"ok": len(reloaded) > 0, "reloaded_clients": reloaded}

    async def set_max_parallel_tabs(self, max_parallel_tabs: int) -> Dict[str, Any]:
        parsed = int(max_parallel_tabs)
        if parsed < 1 or parsed > 200:
            raise ValueError("席位上限范围应为 1~200")
        norm = parsed
        self.max_parallel_tabs = norm
        notified: List[str] = []
        for cid, client in list(self._clients.items()):
            client.client_max_parallel_tabs = norm
            if client.transport == "websocket" and client.websocket is not None:
                if await self._safe_ws_send(
                    client,
                    {"type": "set_max_parallel_tabs", "max_parallel_tabs": norm},
                ):
                    notified.append(cid)
        self._parallel_limit.wake_waiters()
        self.add_log("info", f"工作席位上限已调整为：{norm} 席")
        return {
            "ok": True,
            "max_parallel_tabs": norm,
            "unlimited": False,
            "notified_clients": notified,
        }

    def get_affinity_slot_key(self, explicit_session_id: Optional[str]) -> Optional[str]:
        if not explicit_session_id:
            return None
        key = self._session_affinity.get(explicit_session_id)
        slot = self._slots.get(key)
        if slot is not None and slot.pinned_session_id == explicit_session_id:
            return key
        self._session_affinity.pop(explicit_session_id, None)
        return None

    def _is_slot_occupied(self, slot: WorkerSlot) -> bool:
        if (
            self.is_slot_ephemeral_probe is not None
            and self.is_slot_ephemeral_probe(slot.slot_key)
            and not slot.pinned_session_id
        ):
            return False
        if slot.pinned_session_id:
            return True
        if self.has_slot_conversation is not None:
            return bool(self.has_slot_conversation(slot.slot_key))
        if slot.last_used_at > 0.0:
            return True
        if extract_vertex_session_id(slot.tab_url):
            return True
        return False

    def _is_slot_holding_protected_session(
        self,
        slot: WorkerSlot,
        explicit_session_id: Optional[str],
        now: float,
        requester_tenant_id: Optional[str] = None,
    ) -> bool:
        if now < slot.pending_tool_call_until:
            return True
        if (
            self.session_affinity_ttl_seconds > 0
            and slot.owner_tenant_id
            and slot.owner_tenant_id != "host"
            and not slot.is_busy
            and slot.waiting_count == 0
            and slot.last_used_at > 0.0
            and (now - slot.last_used_at) > self.session_affinity_ttl_seconds
        ):
            self._evict_slot_binding(slot)
            return False
        if slot.owner_tenant_id and requester_tenant_id and slot.owner_tenant_id != requester_tenant_id:
            return True
        if slot.pinned_session_id and slot.pinned_session_id != explicit_session_id:
            return True
        if self._is_slot_occupied(slot):
            return True
        return False

    @contextlib.asynccontextmanager
    async def acquire_slot(
        self,
        explicit_session_id: Optional[str] = None,
        matching_slot_keys: Optional[Sequence[str]] = None,
        queue_timeout: float = 300.0,
        ephemeral_aux: bool = False,
        preferred_session_url: Optional[str] = None,
        ephemeral_probe: bool = False,
        tenant_id: str = "host",
        avoid_affinity_slot: bool = False,
        strict_session_affinity: bool = False,
        avoid_slot_key: Optional[str] = None,
    ) -> AsyncIterator[WorkerSlot]:
        """Atomically select/spawn and lock an isolated `WorkerSlot` for parallel execution."""
        loop = asyncio.get_running_loop()
        queue_deadline = loop.time() + queue_timeout
        thread_lock = None
        if strict_session_affinity and explicit_session_id and not (ephemeral_aux or ephemeral_probe):
            thread_lock = self._thread_locks.get(explicit_session_id)
            if thread_lock is None:
                thread_lock = self._thread_locks[explicit_session_id] = asyncio.Lock()
            try:
                await asyncio.wait_for(thread_lock.acquire(), timeout=max(0, queue_deadline - loop.time()))
            except asyncio.TimeoutError as exc:
                self._total_errors += 1
                raise BridgeTimeoutError(f"等待同一 Codex 会话的上一轮执行结束超时（>{queue_timeout:.0f}s）。") from exc
        try:
            async with self._acquire_slot(
                explicit_session_id, matching_slot_keys, queue_deadline, queue_timeout,
                ephemeral_aux, preferred_session_url, ephemeral_probe, tenant_id,
                avoid_affinity_slot and thread_lock is None, thread_lock is not None,
                avoid_slot_key=avoid_slot_key if thread_lock is None else None,
            ) as slot:
                yield slot
        finally:
            if thread_lock is not None:
                thread_lock.release()

    @contextlib.asynccontextmanager
    async def _acquire_slot(
        self,
        explicit_session_id: Optional[str],
        matching_slot_keys: Optional[Sequence[str]],
        queue_deadline: float,
        queue_timeout: float,
        ephemeral_aux: bool,
        preferred_session_url: Optional[str],
        ephemeral_probe: bool,
        tenant_id: str,
        avoid_affinity_slot: bool,
        strict_session_affinity: bool,
        avoid_slot_key: Optional[str] = None,
    ) -> AsyncIterator[WorkerSlot]:
        chosen: Optional[WorkerSlot] = None
        spawn_target: Optional[tuple[BrowserClientState, str]] = None
        is_ephemeral = bool(ephemeral_aux or ephemeral_probe)
        effective_session_id = None if is_ephemeral else explicit_session_id
        effective_tenant_id = None if is_ephemeral else (tenant_id or "host")
        preferred_vsid = "" if is_ephemeral else extract_vertex_session_id(preferred_session_url or "")
        reused_existing_session = False

        if not self.has_ready_client():
            rem_wait = max(0.0, queue_deadline - asyncio.get_running_loop().time())
            await self.wait_for_ready_client(timeout=min(self.ws_reconnect_wait_seconds, rem_wait))

        async with self._routing_lock:
            ready_slots = self._get_alive_ready_slots()

            if not ready_slots:
                spawn_zero = self._find_spawnable_client([])
                if spawn_zero is not None:
                    spawn_client, spawn_url = spawn_zero
                    spawn_client.spawning_count += 1
                    spawn_target = (
                        spawn_client,
                        preferred_session_url if preferred_vsid and preferred_session_url else spawn_url,
                    )
                else:
                    fallback_client = self.select_active_client()
                    if fallback_client is not None:
                        s = self._upsert_single_slot(
                            client_id=fallback_client.client_id,
                            tab_id=fallback_client.tab_id,
                            tab_url=fallback_client.tab_url,
                            tab_title=fallback_client.tab_title,
                            tab_ready=True,
                            active_model=fallback_client.active_model,
                            captured_rpc=fallback_client.captured_rpc,
                        )
                        ready_slots = [s]

            if not ready_slots and spawn_target is None:
                self._total_errors += 1
                raise BridgeUnavailableError(
                    "未检测到挂载的 Gemini Enterprise 浏览器页面。请打开 "
                    "https://vertexaisearch.cloud.google.com 并确保浏览器扩展或油猴脚本处于已连接状态。"
                )

            if spawn_target is None:
                ready_map = {s.slot_key: s for s in ready_slots}
                now = time.time()
                in_flight_or_waiting = sum(
                    1 for s in ready_slots if s.is_busy or s.waiting_count > 0
                ) + sum(c.spawning_count for c in self._clients.values())
                eff_global_cap = self._effective_global_tab_cap()
                has_parallel_headroom = (eff_global_cap == 0) or (in_flight_or_waiting < eff_global_cap)
                busy_affinity_cand: Optional[WorkerSlot] = None

                valid_matches = (
                    [
                        ready_map[k]
                        for k in matching_slot_keys
                        if k in ready_map
                        and (not strict_session_affinity or ready_map[k].pinned_session_id == effective_session_id)
                        and (
                            not ready_map[k].owner_tenant_id
                            or not effective_tenant_id
                            or ready_map[k].owner_tenant_id == effective_tenant_id
                        )
                    ]
                    if matching_slot_keys
                    else []
                )
                idle_matches = [
                    s for s in valid_matches if not s.is_busy and s.waiting_count == 0 and now >= s.tab_busy_until
                ]

                # 1) Explicit session_id affinity (`X-Session-Id` / `session_id` / `model@session`)
                aff_key = self.get_affinity_slot_key(effective_session_id)
                excluded_slot_key = avoid_slot_key or (aff_key if avoid_affinity_slot else None)
                if strict_session_affinity and aff_key and aff_key not in ready_map and self._slots[aff_key].is_busy:
                    raise BridgeUnavailableError("此 Codex 会话的浏览器页面正在工作，但暂时无法连接，请稍后重试。")
                if aff_key in ready_map:
                    cand = ready_map[aff_key]
                    if not cand.owner_tenant_id or not effective_tenant_id or cand.owner_tenant_id == effective_tenant_id:
                        if strict_session_affinity:
                            chosen = cand
                            reused_existing_session = True
                        elif (
                            not cand.is_busy
                            and cand.waiting_count == 0
                            and now >= cand.tab_busy_until
                            and not avoid_affinity_slot
                            and cand.slot_key != excluded_slot_key
                        ):
                            chosen = idle_matches[0] if (idle_matches and cand not in idle_matches) else cand
                            reused_existing_session = True
                        elif cand.slot_key != excluded_slot_key:
                            busy_affinity_cand = cand

                # 2) Multi-turn conversation history prefix affinity (`matching_slot_keys`)
                if chosen is None and valid_matches:
                    if idle_matches:
                        chosen = idle_matches[0]
                        reused_existing_session = True
                    elif not has_parallel_headroom:
                        chosen = valid_matches[0]
                        reused_existing_session = True
                    elif busy_affinity_cand is None:
                        busy_affinity_cand = valid_matches[0]

                # 2.5) Live tab already sitting on the target archived `/r/session/<id>` URL
                if chosen is None and preferred_vsid:
                    url_matches = [
                        s
                        for s in ready_slots
                        if extract_vertex_session_id(s.tab_url) == preferred_vsid
                        and (not s.owner_tenant_id or not effective_tenant_id or s.owner_tenant_id == effective_tenant_id)
                        and (not strict_session_affinity or s.pinned_session_id in (None, effective_session_id))
                    ]
                    if url_matches:
                        idle_url_matches = [
                            s for s in url_matches if not s.is_busy and s.waiting_count == 0 and now >= s.tab_busy_until
                        ]
                        if idle_url_matches:
                            chosen = idle_url_matches[0]
                            reused_existing_session = True
                        elif not has_parallel_headroom:
                            chosen = url_matches[0]
                            reused_existing_session = True

                # 3) Truly blank/unoccupied idle slot not holding any prior conversation
                if chosen is None:
                    clean_idle = [
                        s
                        for s in ready_slots
                        if not s.is_busy
                        and s.waiting_count == 0
                        and now >= s.tab_busy_until
                        and s.slot_key != excluded_slot_key
                        and not self._is_slot_holding_protected_session(
                            s, effective_session_id, now, requester_tenant_id=effective_tenant_id
                        )
                    ]
                    if clean_idle:
                        def _idle_rank(s: WorkerSlot) -> tuple[int, int, float]:
                            is_probe = bool(
                                self.is_slot_ephemeral_probe is not None
                                and self.is_slot_ephemeral_probe(s.slot_key)
                            )
                            is_blank = not bool(extract_vertex_session_id(s.tab_url))
                            if ephemeral_probe:
                                tier = 0 if is_probe else (1 if is_blank else 2)
                            else:
                                tier = 0 if is_blank else (1 if is_probe else 2)
                            return (tier, 0 if s.last_used_at == 0.0 else 1, s.last_used_at)

                        clean_idle.sort(key=_idle_rank)
                        chosen = clean_idle[0]

                # 4) Auto-spawn a new background worker tab before evicting any occupied session slot
                if chosen is None:
                    spawn_candidate = self._find_spawnable_client(ready_slots)
                    if spawn_candidate is not None:
                        spawn_client, spawn_url = spawn_candidate
                        spawn_client.spawning_count += 1
                        spawn_target = (
                            spawn_client,
                            preferred_session_url if preferred_vsid and preferred_session_url else spawn_url,
                        )

                # 5) Pool at max_parallel_tabs (or spawn unavailable): pick true LRU idle slot, else least-loaded busy slot
                if chosen is None and spawn_target is None:
                    fallback_pool = (
                        [s for s in ready_slots if s.slot_key != excluded_slot_key]
                        if (excluded_slot_key and len(ready_slots) > 1)
                        else ready_slots
                    )
                    non_busy_flagged = [s for s in fallback_pool if now >= s.tab_busy_until]
                    fallback = self._select_fallback_slot(
                        non_busy_flagged or fallback_pool,
                        now,
                        requester_tenant_id=effective_tenant_id or "host",
                    )
                    if (
                        busy_affinity_cand is not None
                        and not avoid_affinity_slot
                        and busy_affinity_cand.slot_key != excluded_slot_key
                        and (fallback.is_busy or fallback.waiting_count > 0)
                        and (busy_affinity_cand.waiting_count + (1 if busy_affinity_cand.is_busy else 0))
                        <= (fallback.waiting_count + (1 if fallback.is_busy else 0))
                    ):
                        chosen = busy_affinity_cand
                        reused_existing_session = True
                    else:
                        chosen = fallback

            if chosen is not None:
                chosen.waiting_count += 1
                if effective_session_id and (
                    reused_existing_session
                    or (not chosen.is_busy and chosen.waiting_count == 1 and not self._is_slot_occupied(chosen))
                ):
                    existing_aff = self.get_affinity_slot_key(effective_session_id)
                    if existing_aff is None or existing_aff == chosen.slot_key or reused_existing_session:
                        self._session_affinity[effective_session_id] = chosen.slot_key
                    chosen.pinned_session_id = effective_session_id
                    if effective_tenant_id:
                        chosen.owner_tenant_id = effective_tenant_id

        # Perform async tab auto-spawn outside `_routing_lock` so other parallel requests aren't stalled!
        if spawn_target is not None:
            spawn_client, spawn_url = spawn_target
            spawned_slot = None
            try:
                if preferred_vsid and preferred_session_url:
                    spawned_slot = await self._spawn_worker_tab(
                        spawn_client,
                        spawn_url,
                        preserve_session_url=True,
                    )
                else:
                    spawned_slot = await self._spawn_worker_tab(
                        spawn_client,
                        spawn_url,
                    )
            finally:
                async with self._routing_lock:
                    spawn_client.spawning_count = max(0, spawn_client.spawning_count - 1)
            # Cancellation must not reserve a slot after the caller has left.
            async with self._routing_lock:
                if spawned_slot is not None:
                    chosen = spawned_slot
                    if preferred_vsid and extract_vertex_session_id(chosen.tab_url) == preferred_vsid:
                        reused_existing_session = True
                else:
                    fallback_slots = self._get_alive_ready_slots()
                    if not fallback_slots:
                        self._total_errors += 1
                        raise BridgeUnavailableError("浏览器标签页池不可用，自动扩容失败。")
                    chosen = self._select_fallback_slot(
                        fallback_slots, time.time(), requester_tenant_id=effective_tenant_id or "host"
                    )
                chosen.waiting_count += 1
                if effective_session_id and (
                    reused_existing_session
                    or (not chosen.is_busy and chosen.waiting_count == 1 and not self._is_slot_occupied(chosen))
                ):
                    existing_aff = self.get_affinity_slot_key(effective_session_id)
                    if existing_aff is None or existing_aff == chosen.slot_key or reused_existing_session:
                        self._session_affinity[effective_session_id] = chosen.slot_key
                    chosen.pinned_session_id = effective_session_id
                    if effective_tenant_id:
                        chosen.owner_tenant_id = effective_tenant_id

        assert chosen is not None
        acquired = permit_acquired = False
        yielded = False
        waiting = True
        try:
            try:
                await asyncio.wait_for(
                    chosen.lock.acquire(),
                    timeout=max(0, queue_deadline - asyncio.get_running_loop().time()),
                )
                acquired = True
                await asyncio.wait_for(
                    self._parallel_limit.acquire(),
                    timeout=max(0, queue_deadline - asyncio.get_running_loop().time()),
                )
                permit_acquired = True
            except asyncio.TimeoutError as exc:
                self._total_errors += 1
                raise BridgeTimeoutError(
                    f"等待浏览器工作标签页超时（>{queue_timeout:.0f}s），并行会话槽位繁忙。"
                ) from exc
            if self._slots.get(chosen.slot_key) is not chosen or not chosen.tab_ready:
                raise BridgeUnavailableError("排队期间工作页面已关闭或连接已重建，请重试。")
            chosen.waiting_count = max(0, chosen.waiting_count - 1)
            waiting = False
            async with self._routing_lock:
                same_pinned = bool(
                    effective_session_id
                    and chosen.pinned_session_id == effective_session_id
                    and (not chosen.owner_tenant_id or not effective_tenant_id or chosen.owner_tenant_id == effective_tenant_id)
                )
                cross_tenant = bool(
                    effective_tenant_id
                    and chosen.owner_tenant_id
                    and chosen.owner_tenant_id != effective_tenant_id
                )
                was_probe = bool(
                    self.is_slot_ephemeral_probe is not None
                    and self.is_slot_ephemeral_probe(chosen.slot_key)
                )
                if cross_tenant or (not reused_existing_session and not same_pinned and (
                    self._is_slot_occupied(chosen) or chosen.pinned_session_id or was_probe
                )):
                    self._evict_slot_binding(chosen)
                if effective_session_id:
                    existing_aff = self.get_affinity_slot_key(effective_session_id)
                    if strict_session_affinity or existing_aff is None or existing_aff == chosen.slot_key or reused_existing_session:
                        self._session_affinity[effective_session_id] = chosen.slot_key
                    chosen.pinned_session_id = effective_session_id
                if effective_tenant_id:
                    chosen.owner_tenant_id = effective_tenant_id
            yielded = True
            yield chosen
        finally:
            if waiting:
                chosen.waiting_count = max(0, chosen.waiting_count - 1)
            if acquired:
                if yielded:
                    chosen.last_used_at = time.time()
                chosen.in_flight_request_id = None
                chosen.lock.release()
            if permit_acquired:
                self._parallel_limit.release()

    def _select_fallback_slot(
        self,
        ready_slots: Sequence[WorkerSlot],
        now: float,
        requester_tenant_id: str = "host",
    ) -> WorkerSlot:
        def _tenant_tier(s: WorkerSlot) -> int:
            if not self._is_slot_occupied(s) and not s.owner_tenant_id:
                return 0
            owner = s.owner_tenant_id or "host"
            if requester_tenant_id != "host":
                if owner == requester_tenant_id:
                    return 1
                if owner != "host":
                    return 2
                return 3
            return 1 if owner != "host" else 2

        idle_slots = [s for s in ready_slots if not s.is_busy and s.waiting_count == 0]
        if idle_slots:
            idle_slots.sort(
                key=lambda s: (
                    1 if (now < s.pending_tool_call_until or self._is_slot_codex_active(s, now)) else 0,
                    _tenant_tier(s),
                    s.last_used_at,
                )
            )
            return idle_slots[0]
        ranked = sorted(
            ready_slots,
            key=lambda s: (
                s.waiting_count + (1 if s.is_busy else 0),
                1 if (requester_tenant_id != "host" and (s.owner_tenant_id or "host") == "host") else 0,
                s.last_used_at,
            ),
        )
        return ranked[0]

    # ------------------------------------------------------------------
    # Multimodal Attachment Staging (Token-Guarded Local Binary Store)
    # ------------------------------------------------------------------

    def prune_expired_attachments(self) -> None:
        now = time.time()
        for att_id, item in list(self._staged_attachments.items()):
            if item.expires_at <= now:
                self._staged_attachments.pop(att_id, None)

    async def stage_attachments(
        self,
        attachments: Sequence[Any],
        ttl_seconds: float = ATTACHMENT_STAGE_TTL_SECONDS,
    ) -> List[Dict[str, Any]]:
        self.prune_expired_attachments()
        if not attachments:
            return []
        now = time.time()
        expires_at = now + max(60.0, ttl_seconds)
        staged_list: List[Dict[str, Any]] = []
        for att in attachments:
            if isinstance(att, dict):
                if att.get("fetch_url") or att.get("file_id"):
                    copy_entry = dict(att)
                    copy_entry.setdefault("filename", str(att.get("name") or "attachment.bin"))
                    copy_entry.setdefault("name", copy_entry["filename"])
                    copy_entry.setdefault("kind", str(att.get("category") or "file"))
                    copy_entry.setdefault("media_kind", copy_entry["kind"])
                    if copy_entry.get("fetch_url") and not copy_entry.get("path"):
                        parsed_u = urllib.parse.urlparse(str(copy_entry["fetch_url"]))
                        copy_entry["path"] = parsed_u.path + (f"?{parsed_u.query}" if parsed_u.query else "")
                        copy_entry.setdefault("url", copy_entry["fetch_url"])
                    staged_list.append(copy_entry)
                    continue
                fname = str(att.get("name") or att.get("filename") or "attachment.bin")
                mime = str(att.get("mime_type") or "application/octet-stream")
                cat = str(att.get("category") or att.get("kind") or att.get("media_kind") or "file")
                data_url = str(att.get("data_url") or "")
                if data_url and "," in data_url:
                    raw_bytes = base64.b64decode(data_url.split(",", 1)[1])
                elif att.get("_path") and Path(str(att["_path"])).is_file():
                    raw_bytes = Path(str(att["_path"])).read_bytes()
                else:
                    raw_bytes = b""
                sha = hashlib.sha256(raw_bytes).hexdigest()
            else:
                try:
                    raw_bytes = await att.async_resolve_bytes()
                except Exception as exc:
                    raise BridgeExecutionError(f"无法读取或下载多模态附件 '{att.filename}': {exc}") from exc
                fname = att.filename
                mime = att.mime_type
                cat = att.kind
                sha = att.sha256
                data_url = (
                    f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
                    if len(raw_bytes) <= 1024 * 1024
                    else ""
                )
            att_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(24)
            record = StagedAttachment(
                attachment_id=att_id,
                token=token,
                filename=fname,
                mime_type=mime,
                data=raw_bytes,
                sha256=sha,
                created_at=now,
                expires_at=expires_at,
            )
            self._staged_attachments[att_id] = record
            rel_path = f"/api/bridge/attachments/{att_id}?token={urllib.parse.quote(token, safe='')}"
            full_url = f"{self.callback_base_url}{rel_path}" if self.callback_base_url else rel_path
            entry: Dict[str, Any] = {
                "id": att_id,
                "marker": att.get("id", "") if isinstance(att, dict) else "",
                "name": fname,
                "filename": fname,
                "mime_type": mime,
                "category": cat,
                "kind": cat,
                "media_kind": cat,
                "size_bytes": len(raw_bytes),
                "sha256": sha,
                "url": full_url,
                "path": rel_path,
            }
            if not isinstance(att, dict):
                entry["id"] = att_id
            if data_url:
                entry["data_url"] = data_url
            staged_list.append(entry)
        return staged_list

    def verify_staged_attachment(self, attachment_id: str, token: str) -> tuple[Optional[StagedAttachment], str]:
        self.prune_expired_attachments()
        item = self._staged_attachments.get(attachment_id)
        if item is None:
            return None, "not_found"
        if not token or not secrets.compare_digest(item.token, token):
            return None, "forbidden"
        return item, "ok"

    def get_staged_attachment(self, attachment_id: str, token: str) -> Optional[StagedAttachment]:
        item, _ = self.verify_staged_attachment(attachment_id, token)
        return item

    # ------------------------------------------------------------------
    # Prompt Dispatch & Streaming Pipeline (Per-Slot Parallel Execution)
    # ------------------------------------------------------------------

    async def dispatch_stream_on_slot(
        self,
        slot: WorkerSlot,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 600.0,
        attachments: Optional[Sequence[Any]] = None,
        image_generation: bool = False,
        video_generation: bool = False,
        output_images: Optional[List[Dict[str, Any]]] = None,
        output_files: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[str]:
        """Execute a prompt on an already-acquired `WorkerSlot` without blocking other slots."""
        self._total_requests += 1
        slot.last_dispatch_submitted = False
        slot.last_dispatch_tab_reset = False
        sanitized_prompt = prompt.replace("\x00", "")
        req_id = uuid.uuid4().hex
        client = self._clients.get(slot.client_id)
        if (client is None or not client.is_alive()) and await self.wait_for_client_reconnect(slot.client_id):
            client = self._clients.get(slot.client_id)
        if client is None or not client.is_alive():
            self._total_errors += 1
            raise BridgeUnavailableError("目标浏览器工作标签页所属连接已断开。")

        slot.in_flight_request_id = req_id
        completed_normally = False

        try:
            staged_attachments = (
                await self.stage_attachments(attachments, ttl_seconds=max(request_timeout, ATTACHMENT_STAGE_TTL_SECONDS))
                if attachments
                else []
            )
            logger.debug(
                "Dispatching request=%s slot=%s tab=%s model=%s reset=%s prompt_chars=%d attachments=%d",
                req_id[:8],
                slot.slot_key,
                slot.tab_id,
                target_model or "auto",
                reset_chat,
                len(sanitized_prompt),
                len(staged_attachments),
            )
            inflight = InFlightRequest(
                request_id=req_id,
                client_id=client.client_id,
                slot_key=slot.slot_key,
                tab_id=slot.tab_id,
                prompt=sanitized_prompt,
                reset_chat=reset_chat,
                stream=True,
                target_model=target_model,
            )
            self._pending[req_id] = inflight

            callback_url = (
                f"{self.callback_base_url}/api/bridge/callback"
                if self.callback_base_url
                else ""
            )

            if len(sanitized_prompt) > PROMPT_CHUNK_THRESHOLD:
                slices = [
                    sanitized_prompt[i : i + PROMPT_CHUNK_SIZE]
                    for i in range(0, len(sanitized_prompt), PROMPT_CHUNK_SIZE)
                ]
                total_chunks = len(slices)
                for idx, chunk_str in enumerate(slices):
                    chunk_cmd: Dict[str, Any] = {
                        "type": "execute_prompt_chunk",
                        "request_id": req_id,
                        "chunk_index": idx,
                        "total_chunks": total_chunks,
                        "data": chunk_str,
                    }
                    if slot.tab_id is not None:
                        chunk_cmd["tab_id"] = slot.tab_id
                    await self._send_command_to_client(client, chunk_cmd)

                command: Dict[str, Any] = {
                    "type": "execute_prompt",
                    "request_id": req_id,
                    "prompt": "",
                    "chunked": True,
                    "total_chunks": total_chunks,
                    "target_model": target_model,
                    "reset_chat": reset_chat,
                    "stream": True,
                    "timeout_ms": int(request_timeout * 1000),
                    "callback_url": callback_url,
                }
            else:
                command = {
                    "type": "execute_prompt",
                    "request_id": req_id,
                    "prompt": sanitized_prompt,
                    "target_model": target_model,
                    "reset_chat": reset_chat,
                    "stream": True,
                    "timeout_ms": int(request_timeout * 1000),
                    "callback_url": callback_url,
                }
            command["attachments"] = staged_attachments
            command["image_generation"] = image_generation
            command["video_generation"] = video_generation
            if slot.tab_id is not None:
                command["tab_id"] = slot.tab_id

            await self._send_command_to_client(client, command)

            emitted_parts: List[str] = []
            sliding_window = request_timeout
            progress_dom = progress_net = 0
            deadline = time.monotonic() + request_timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    slot.last_dispatch_submitted = True
                    self._total_errors += 1
                    raise BridgeTimeoutError(f"网页端响应超时（>{request_timeout:.0f}s）。")

                try:
                    event = await asyncio.wait_for(
                        inflight.events.get(),
                        timeout=min(remaining, 15.0),
                    )
                except asyncio.TimeoutError:
                    # Yield empty string heartbeat so SSE streams can emit `: keepalive` comments
                    yield ""
                    continue

                ev_type = event.get("type")
                if ev_type == "progress":
                    dom_len = max(0, int(event.get("dom_len") or 0))
                    net_len = max(0, int(event.get("net_len") or 0))
                    if dom_len > progress_dom or net_len > progress_net:
                        deadline = max(deadline, time.monotonic() + sliding_window)
                    progress_dom = max(progress_dom, dom_len)
                    progress_net = max(progress_net, net_len)
                    yield ""
                    continue
                if ev_type == "chunk":
                    delta = event.get("delta", "")
                    if delta:
                        emitted_parts.append(delta)
                        yield delta
                    deadline = max(deadline, time.monotonic() + sliding_window)
                elif ev_type == "error":
                    raise BridgeExecutionError(str(event.get("message") or "网页端执行失败"))

                elif ev_type == "done":
                    if output_images is not None:
                        output_images.extend(event.get("output_images") or [])
                    if output_files is not None:
                        output_files.extend(event.get("output_files") or [])
                    final_reply = str(event.get("reply") or "")
                    emitted_so_far = "".join(emitted_parts)

                    if not emitted_so_far and final_reply:
                        yield final_reply
                        emitted_so_far = final_reply
                    elif final_reply and final_reply.startswith(emitted_so_far):
                        tail = final_reply[len(emitted_so_far) :]
                        if tail:
                            yield tail
                    elif final_reply != emitted_so_far:
                        common = 0
                        for left, right in zip(emitted_so_far, final_reply):
                            if left != right:
                                break
                            common += 1
                        self._total_errors += 1
                        self.add_log("error", f"Reply integrity request={req_id[:8]} source={event.get('source')} streamed={len(emitted_so_far)} final={len(final_reply)} common_prefix={common}", client.client_id)
                        raise BridgeExecutionError("网页最终回复与已接收内容不一致，未将截断内容标为完成。")

                    completed_normally = True
                    self.add_log("info", f"Reply complete request={req_id[:8]} source={event.get('source')} streamed={len(emitted_so_far)} final={len(final_reply)}", client.client_id)
                    citations = event.get("citations") or []
                    if self.include_citations and not suppress_citations and citations:
                        formatted_refs = self._format_citations(citations, final_reply or emitted_so_far)
                        if formatted_refs:
                            yield formatted_refs
                    break

        finally:
            self._pending.pop(req_id, None)
            slot.in_flight_request_id = None
            if not completed_normally and client is not None:
                await self._send_cancel_safely(client, req_id, tab_id=slot.tab_id)

    async def dispatch_complete_on_slot(
        self,
        slot: WorkerSlot,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 600.0,
        attachments: Optional[List[Dict[str, Any]]] = None,
        image_generation: bool = False,
        video_generation: bool = False,
        output_images: Optional[List[Dict[str, Any]]] = None,
        output_files: Optional[List[Dict[str, Any]]] = None,
        strip_reply: bool = True,
    ) -> str:
        chunks: List[str] = []
        async for delta in self.dispatch_stream_on_slot(
            slot=slot,
            prompt=prompt,
            target_model=target_model,
            reset_chat=reset_chat,
            suppress_citations=suppress_citations,
            request_timeout=request_timeout,
            attachments=attachments,
            image_generation=image_generation,
            video_generation=video_generation,
            output_images=output_images,
            output_files=output_files,
        ):
            if delta:
                chunks.append(delta)
        joined = "".join(chunks)
        return joined.strip() if strip_reply else joined

    async def dispatch_stream(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 600.0,
        queue_timeout: float = 300.0,
        explicit_session_id: Optional[str] = None,
        matching_slot_keys: Optional[Sequence[str]] = None,
    ) -> AsyncIterator[str]:
        """Acquire a parallel worker slot and stream chunks."""
        async with self.acquire_slot(
            explicit_session_id=explicit_session_id,
            matching_slot_keys=matching_slot_keys,
            queue_timeout=queue_timeout,
        ) as slot:
            async for delta in self.dispatch_stream_on_slot(
                slot=slot,
                prompt=prompt,
                target_model=target_model,
                reset_chat=reset_chat,
                suppress_citations=suppress_citations,
                request_timeout=request_timeout,
            ):
                yield delta

    async def dispatch_complete(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 600.0,
        queue_timeout: float = 300.0,
        explicit_session_id: Optional[str] = None,
        matching_slot_keys: Optional[Sequence[str]] = None,
    ) -> str:
        chunks: List[str] = []
        async for delta in self.dispatch_stream(
            prompt=prompt,
            target_model=target_model,
            reset_chat=reset_chat,
            suppress_citations=suppress_citations,
            request_timeout=request_timeout,
            queue_timeout=queue_timeout,
            explicit_session_id=explicit_session_id,
            matching_slot_keys=matching_slot_keys,
        ):
            if delta:
                chunks.append(delta)
        return "".join(chunks).strip()

    async def _send_command_to_client(self, client: BrowserClientState, command: Dict[str, Any]) -> None:
        if client.transport == "websocket":
            websocket = client.websocket
            ok = await self._safe_ws_send(client, command)
            if not ok:
                if websocket is not None:
                    self.unregister_client(client.client_id, reason="ws_send_failed", websocket=websocket)
                if await self.wait_for_client_reconnect(client.client_id):
                    reconnected = self._clients.get(client.client_id)
                    if reconnected is not None and await self._safe_ws_send(reconnected, command):
                        return
                raise BridgeUnavailableError("浏览器扩展 WebSocket 发送失败，连接已断开。")
        else:
            await client.command_queue.put(command)

    async def _send_cancel_safely(
        self,
        client: BrowserClientState,
        request_id: str,
        tab_id: Optional[int] = None,
    ) -> None:
        cancel_cmd: Dict[str, Any] = {"type": "cancel_request", "request_id": request_id}
        if tab_id is not None:
            cancel_cmd["tab_id"] = tab_id
        try:
            if client.transport == "websocket":
                latest_client = self._clients.get(client.client_id) or client
                sent = await self._safe_ws_send(latest_client, cancel_cmd)
                if not sent:
                    latest_client.pending_cancels.append((request_id, tab_id, False))
                    if len(latest_client.pending_cancels) > 32:
                        latest_client.pending_cancels = latest_client.pending_cancels[-32:]
            else:
                client.command_queue.put_nowait(cancel_cmd)
        except Exception:
            pass

    @staticmethod
    async def _safe_ws_send(client: BrowserClientState, payload: Dict[str, Any]) -> bool:
        if client.websocket is None:
            return False
        try:
            async with client.ws_send_lock:
                if client.websocket is None:
                    return False
                await client.websocket.send_json(payload)
            return True
        except Exception:
            return False

    @staticmethod
    def _format_citations(citations: List[Dict[str, Any]], body_text: str) -> str:
        seen_urls = set()
        items: List[str] = []
        for item in citations:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("uri") or "").strip()
            title = str(item.get("title") or url).strip()
            if not url or url in seen_urls or url in body_text:
                continue
            seen_urls.add(url)
            items.append(f"- [{title}]({url})")
        if not items:
            return ""
        return "\n\n---\n**参考来源 (Enterprise Grounding):**\n" + "\n".join(items[:8])

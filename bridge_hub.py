"""Multi-Tab Parallel Browser Bridge Hub with Sticky Session Routing, Auto-Spawn, and Live Streaming."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Set

from fastapi import WebSocket

from updater import compare_semver, normalize_version
from version import APP_VERSION

logger = logging.getLogger("gemini_bridge.hub")


class BridgeUnavailableError(RuntimeError):
    """Raised when no active Gemini Enterprise browser client is connected."""


class BridgeTimeoutError(TimeoutError):
    """Raised when waiting for a worker tab or browser reply times out."""


class BridgeExecutionError(RuntimeError):
    """Raised when the browser driver reports an execution failure."""


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
    pending_tool_call_until: float = 0.0
    in_flight_request_id: Optional[str] = None
    waiting_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_busy(self) -> bool:
        return self.lock.locked() or self.in_flight_request_id is not None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "client_id": self.client_id,
            "tab_id": self.tab_id,
            "tab_url": self.tab_url,
            "tab_title": self.tab_title,
            "tab_ready": self.tab_ready,
            "active_model": self.active_model,
            "captured_rpc": self.captured_rpc,
            "busy": self.is_busy,
            "waiting_count": self.waiting_count,
            "pinned_session_id": self.pinned_session_id,
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


PROMPT_CHUNK_THRESHOLD = 32768
PROMPT_CHUNK_SIZE = 24576


class BrowserBridgeHub:
    """Coordinates parallel browser worker tabs, sticky session routing, auto-spawning, and live streaming."""

    def __init__(
        self,
        include_citations: bool = True,
        callback_base_url: str = "",
        max_parallel_tabs: int = 6,
        auto_spawn_tabs: bool = True,
        session_affinity_ttl_seconds: float = 300.0,
        backend_version: str = APP_VERSION,
    ) -> None:
        self.include_citations = include_citations
        self.callback_base_url = callback_base_url.rstrip("/")
        self.max_parallel_tabs = max(1, max_parallel_tabs)
        self.auto_spawn_tabs = auto_spawn_tabs
        self.session_affinity_ttl_seconds = max(0.0, session_affinity_ttl_seconds)
        self.backend_version = normalize_version(backend_version) or APP_VERSION
        self._clients: Dict[str, BrowserClientState] = {}
        self._slots: Dict[str, WorkerSlot] = {}
        self._session_affinity: Dict[str, str] = {}  # explicit_session_id -> slot_key
        self._pending: Dict[str, InFlightRequest] = {}
        self._pending_spawns: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._routing_lock = asyncio.Lock()
        self._total_requests = 0
        self._total_errors = 0
        self._recent_logs: List[Dict[str, Any]] = []
        self.usage_snapshot_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.on_slot_removed: Optional[Callable[[str], None]] = None
        self.on_slot_migrated: Optional[Callable[[str, str], None]] = None
        self.on_extension_version_reported: Optional[Callable[[str], Any]] = None
        self.on_outdated_extension_detected: Optional[Callable[[str], Any]] = None

    def add_log(self, level: str, message: str, client_id: Optional[str] = None) -> None:
        entry = {
            "ts": int(time.time()),
            "level": level,
            "client_id": client_id,
            "message": message[:500],
        }
        self._recent_logs.append(entry)
        if len(self._recent_logs) > 80:
            self._recent_logs = self._recent_logs[-80:]

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
                slot.tab_url = tab_url
            if tab_title:
                slot.tab_title = tab_title
            slot.tab_ready = tab_ready
            if active_model:
                slot.active_model = active_model
            if captured_rpc is not None:
                slot.captured_rpc = bool(captured_rpc)
        return slot

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

        for item in tabs_payload:
            if not isinstance(item, dict):
                continue
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
            seen_keys.add(slot.slot_key)

        # Prune slots belonging to `client_id` that were closed in the browser
        for existing_key, existing_slot in list(self._slots.items()):
            if existing_slot.client_id == client_id and existing_key not in seen_keys:
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

    # ------------------------------------------------------------------
    # Client registration & lifecycle
    # ------------------------------------------------------------------

    async def register_websocket(self, websocket: WebSocket, client_id: Optional[str] = None) -> BrowserClientState:
        cid = client_id or f"ws-{uuid.uuid4().hex[:8]}"
        prev = self._clients.get(cid)
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
        )
        self._clients[cid] = state
        if prev is not None and prev.websocket is not None and prev.websocket is not websocket:
            try:
                await prev.websocket.close(code=1000)
            except Exception:
                pass
        self.add_log("info", f"Extension WebSocket connected ({cid})", cid)
        welcome_payload: Dict[str, Any] = {
            "type": "welcome",
            "client_id": cid,
            "server_time": int(time.time()),
            "backend_version": self.backend_version,
            "max_parallel_tabs": self.max_parallel_tabs,
            "auto_spawn_tabs": self.auto_spawn_tabs,
        }
        if self.usage_snapshot_provider is not None:
            try:
                welcome_payload["usage_snapshot"] = self.usage_snapshot_provider()
            except Exception:
                pass
        await self._safe_ws_send(state, welcome_payload)
        return state

    async def broadcast_ws_event(
        self,
        payload: Dict[str, Any],
        required_capability: Optional[str] = "usage_push",
    ) -> None:
        """Broadcast a JSON event frame (such as `usage_update`) to live WebSocket clients opting in."""
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
    ) -> None:
        existing = self._clients.get(client_id)
        if existing is None:
            return
        if websocket is not None and existing.transport == "websocket" and existing.websocket is not websocket:
            return
        state = self._clients.pop(client_id, None)
        if state is not None:
            state.websocket = None
            self.add_log("warn", f"Browser client {client_id} disconnected ({reason})", client_id)

        for skey, slot in list(self._slots.items()):
            if slot.client_id == client_id:
                self._remove_slot(skey, reason=reason)

        for req_id, inflight in list(self._pending.items()):
            if inflight.client_id == client_id:
                inflight.events.put_nowait(
                    {
                        "type": "error",
                        "message": f"浏览器桥接连接在生成过程中断开 ({reason})，请确认页面未关闭。",
                    }
                )
                self._pending.pop(req_id, None)

    def register_http_client(
        self,
        client_id: Optional[str] = None,
        user_agent: str = "",
        tab_id: Optional[int] = None,
        tab_url: str = "",
        tab_title: str = "",
        driver_mode: str = "auto",
        active_model: str = "",
    ) -> BrowserClientState:
        self._prune_stale_http_clients()
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

    def has_ready_client(self) -> bool:
        return len(self._get_alive_ready_slots()) > 0 or self.select_active_client() is not None

    def status_snapshot(self) -> Dict[str, Any]:
        self._prune_stale_http_clients()
        now = time.time()
        alive_states = [c for c in self._clients.values() if c.is_alive(now)]
        clients = [c.to_public_dict() for c in alive_states]
        slots = self._get_alive_ready_slots()
        active = self.select_active_client()
        busy_slots = sum(1 for s in slots if s.is_busy)
        waiting_total = sum(s.waiting_count for s in slots)
        connected_ext_ver = ""
        for st in alive_states:
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
            "worker_slots": [s.to_public_dict() for s in slots],
            "parallel_capacity": len(slots),
            "max_parallel_tabs": self.max_parallel_tabs,
            "auto_spawn_tabs": self.auto_spawn_tabs,
            "active_slots_busy": busy_slots,
            "busy": len(slots) > 0 and busy_slots >= len(slots) and len(slots) >= self.max_parallel_tabs,
            "queued_requests": waiting_total,
            "in_flight_requests": len(self._pending),
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "recent_logs": self._recent_logs[-25:],
        }

    # ------------------------------------------------------------------
    # Incoming browser messages (WebSocket & HTTP bridge unified handler)
    # ------------------------------------------------------------------

    async def handle_browser_event(self, client_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        state = self._clients.get(client_id)
        if state is not None:
            state.last_seen_at = time.time()

        msg_type = str(data.get("type") or data.get("event") or "").strip()

        if msg_type == "spawn_tab_result":
            spawn_id = str(data.get("spawn_id") or "")
            fut = self._pending_spawns.get(spawn_id)
            if fut is not None and not fut.done():
                fut.set_result(data)
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
                if data.get("max_parallel_tabs") is not None:
                    try:
                        state.client_max_parallel_tabs = max(1, int(data["max_parallel_tabs"]))
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

            resp_frame: Dict[str, Any] = {
                "type": "pong" if msg_type == "heartbeat" else "ack",
                "server_time": int(time.time()),
                "backend_version": self.backend_version,
            }
            if backend_outdated_flag:
                resp_frame["backend_update_required"] = True
            if emit_ext_reload:
                resp_frame["_emit_extension_reload"] = self.backend_version
            if self.usage_snapshot_provider is not None:
                try:
                    resp_frame["usage_snapshot"] = self.usage_snapshot_provider()
                except Exception:
                    pass
            return resp_frame

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

        inflight.last_activity_at = time.time()

        if msg_type == "request_ack":
            return None

        if msg_type == "request_progress":
            await inflight.events.put(
                {
                    "type": "progress",
                    "generating": bool(data.get("generating", True)),
                    "dom_len": int(data.get("dom_len") or 0),
                    "net_len": int(data.get("net_len") or 0),
                }
            )
            return None

        if msg_type == "stream_chunk":
            delta = str(data.get("delta") or "")
            if delta:
                await inflight.events.put({"type": "chunk", "delta": delta})
            return None

        if msg_type == "reply_chunk":
            c_idx = int(data.get("chunk_index") or 0)
            c_total = int(data.get("total_chunks") or 0)
            c_data = str(data.get("data") or "")
            inflight.reply_chunks[c_idx] = c_data
            if c_total > 0:
                inflight.expected_reply_chunks = c_total
            return None

        if msg_type == "request_error":
            err_msg = str(data.get("error") or data.get("message") or "浏览器页面执行失败")
            self._total_errors += 1
            self.add_log("error", f"Request {req_id[:8]} error: {err_msg}", client_id)
            await inflight.events.put({"type": "error", "message": err_msg})
            return None

        if msg_type == "request_complete" or "reply" in data:
            if data.get("chunked") and inflight.reply_chunks:
                total_c = int(
                    data.get("total_chunks")
                    or inflight.expected_reply_chunks
                    or len(inflight.reply_chunks)
                )
                reply = "".join(inflight.reply_chunks.get(i, "") for i in range(total_c))
                inflight.reply_chunks.clear()
            else:
                reply = str(data.get("reply") or "")
            citations = data.get("citations") if isinstance(data.get("citations"), list) else []
            source = str(data.get("source") or "browser")

            slot = self._slots.get(inflight.slot_key)
            if slot is not None:
                slot.completed_requests += 1
                if data.get("active_model"):
                    slot.active_model = str(data["active_model"])
            if state is not None:
                state.completed_requests += 1
                if data.get("active_model"):
                    state.active_model = str(data["active_model"])

            await inflight.events.put(
                {
                    "type": "done",
                    "reply": reply,
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
        if client.client_max_parallel_tabs is not None:
            return min(self.max_parallel_tabs, client.client_max_parallel_tabs)
        return self.max_parallel_tabs

    def _find_spawnable_client(self, ready_slots: Sequence[WorkerSlot]) -> Optional[tuple[BrowserClientState, str]]:
        """Find a connected WS extension client capable of spawning another background Vertex tab."""
        if not self.auto_spawn_tabs:
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

    async def _spawn_worker_tab(self, client: BrowserClientState, base_url: str) -> Optional[WorkerSlot]:
        spawn_id = f"sp-{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_spawns[spawn_id] = fut

        self.add_log("info", f"Auto-spawning parallel worker tab ({spawn_id}) ...", client.client_id)
        sent = await self._safe_ws_send(
            client,
            {
                "type": "spawn_tab",
                "spawn_id": spawn_id,
                "base_url": base_url,
            },
        )
        if not sent:
            self._pending_spawns.pop(spawn_id, None)
            return None

        try:
            res = await asyncio.wait_for(fut, timeout=16.0)
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

    def get_affinity_slot_key(self, explicit_session_id: Optional[str]) -> Optional[str]:
        if not explicit_session_id:
            return None
        return self._session_affinity.get(explicit_session_id)

    def _is_slot_holding_protected_session(
        self,
        slot: WorkerSlot,
        explicit_session_id: Optional[str],
        now: float,
    ) -> bool:
        if now < slot.pending_tool_call_until:
            return True
        if slot.pinned_session_id and slot.pinned_session_id != explicit_session_id:
            if slot.last_used_at == 0.0 or (now - slot.last_used_at) < self.session_affinity_ttl_seconds:
                return True
        if slot.last_used_at > 0.0 and (now - slot.last_used_at) < self.session_affinity_ttl_seconds:
            return True
        return False

    @contextlib.asynccontextmanager
    async def acquire_slot(
        self,
        explicit_session_id: Optional[str] = None,
        matching_slot_keys: Optional[Sequence[str]] = None,
        queue_timeout: float = 300.0,
        ephemeral_aux: bool = False,
    ) -> AsyncIterator[WorkerSlot]:
        """Atomically select/spawn and lock an isolated `WorkerSlot` for parallel execution."""
        chosen: Optional[WorkerSlot] = None
        spawn_target: Optional[tuple[BrowserClientState, str]] = None
        effective_session_id = None if ephemeral_aux else explicit_session_id

        async with self._routing_lock:
            ready_slots = self._get_alive_ready_slots()

            # Ensure any alive ready client without an explicit slot yet gets its default slot
            if not ready_slots:
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

            if not ready_slots:
                self._total_errors += 1
                raise BridgeUnavailableError(
                    "未检测到挂载的 Gemini Enterprise 浏览器页面。请打开 "
                    "https://vertexaisearch.cloud.google.com 并确保浏览器扩展或油猴脚本处于已连接状态。"
                )

            ready_map = {s.slot_key: s for s in ready_slots}
            now = time.time()

            # 1) Explicit session_id affinity (`X-Session-Id` / `session_id` / `model@session`)
            if effective_session_id and effective_session_id in self._session_affinity:
                aff_key = self._session_affinity[effective_session_id]
                if aff_key in ready_map:
                    chosen = ready_map[aff_key]

            # 2) Multi-turn conversation history prefix affinity (`matching_slot_keys`)
            if chosen is None and matching_slot_keys:
                valid_matches = [ready_map[k] for k in matching_slot_keys if k in ready_map]
                if valid_matches:
                    idle_matches = [
                        s for s in valid_matches if not s.is_busy and s.waiting_count == 0
                    ]
                    chosen = idle_matches[0] if idle_matches else valid_matches[0]

            # 3) Clean / expired idle slot not holding another active session
            if chosen is None:
                clean_idle = [
                    s
                    for s in ready_slots
                    if not s.is_busy
                    and s.waiting_count == 0
                    and not self._is_slot_holding_protected_session(s, effective_session_id, now)
                ]
                if clean_idle:
                    clean_idle.sort(key=lambda s: (0 if s.last_used_at == 0.0 else 1, s.last_used_at))
                    chosen = clean_idle[0]

            # 4) Auto-spawn a new background worker tab if existing tabs are busy or holding active sessions
            if chosen is None:
                spawn_candidate = self._find_spawnable_client(ready_slots)
                if spawn_candidate is not None:
                    spawn_client, spawn_url = spawn_candidate
                    spawn_client.spawning_count += 1
                    spawn_target = (spawn_client, spawn_url)

            # 5) Pool at max_parallel_tabs (or spawn unavailable): pick LRU idle slot, else least-loaded busy slot
            if chosen is None and spawn_target is None:
                chosen = self._select_fallback_slot(ready_slots, effective_session_id, now)

            if chosen is not None:
                chosen.waiting_count += 1
                if effective_session_id:
                    self._session_affinity[effective_session_id] = chosen.slot_key
                    chosen.pinned_session_id = effective_session_id

        # Perform async tab auto-spawn outside `_routing_lock` so other parallel requests aren't stalled!
        if spawn_target is not None:
            spawn_client, spawn_url = spawn_target
            try:
                spawned_slot = await self._spawn_worker_tab(spawn_client, spawn_url)
            finally:
                async with self._routing_lock:
                    spawn_client.spawning_count = max(0, spawn_client.spawning_count - 1)
                    if spawned_slot is not None:
                        chosen = spawned_slot
                    else:
                        fallback_slots = self._get_alive_ready_slots()
                        if not fallback_slots:
                            self._total_errors += 1
                            raise BridgeUnavailableError("浏览器标签页池不可用，自动扩容失败。")
                        chosen = self._select_fallback_slot(
                            fallback_slots, effective_session_id, time.time()
                        )
                    chosen.waiting_count += 1
                    if effective_session_id:
                        self._session_affinity[effective_session_id] = chosen.slot_key
                        chosen.pinned_session_id = effective_session_id

        assert chosen is not None
        acquired = False
        try:
            try:
                await asyncio.wait_for(chosen.lock.acquire(), timeout=queue_timeout)
                acquired = True
            except asyncio.TimeoutError as exc:
                self._total_errors += 1
                raise BridgeTimeoutError(
                    f"等待浏览器工作标签页超时（>{queue_timeout:.0f}s），所有并行会话槽位均繁忙。"
                ) from exc
        finally:
            chosen.waiting_count = max(0, chosen.waiting_count - 1)

        try:
            yield chosen
        finally:
            chosen.last_used_at = time.time()
            chosen.in_flight_request_id = None
            if acquired:
                chosen.lock.release()

    @staticmethod
    def _select_fallback_slot(
        ready_slots: Sequence[WorkerSlot],
        explicit_session_id: Optional[str],
        now: float,
    ) -> WorkerSlot:
        idle_slots = [s for s in ready_slots if not s.is_busy and s.waiting_count == 0]
        if idle_slots:
            idle_slots.sort(
                key=lambda s: (
                    1 if now < s.pending_tool_call_until else 0,
                    1 if (s.pinned_session_id and s.pinned_session_id != explicit_session_id) else 0,
                    s.last_used_at,
                )
            )
            return idle_slots[0]
        ranked = sorted(
            ready_slots,
            key=lambda s: (s.waiting_count + (1 if s.is_busy else 0), s.last_used_at),
        )
        return ranked[0]

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
    ) -> AsyncIterator[str]:
        """Execute a prompt on an already-acquired `WorkerSlot` without blocking other slots."""
        self._total_requests += 1
        sanitized_prompt = prompt.replace("\x00", "")
        req_id = uuid.uuid4().hex
        client = self._clients.get(slot.client_id)
        if client is None or not client.is_alive():
            self._total_errors += 1
            raise BridgeUnavailableError("目标浏览器工作标签页所属连接已断开。")

        slot.in_flight_request_id = req_id
        completed_normally = False

        try:
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
            if slot.tab_id is not None:
                command["tab_id"] = slot.tab_id

            await self._send_command_to_client(client, command)

            emitted_parts: List[str] = []
            sliding_window = max(request_timeout, 180.0)
            deadline = time.monotonic() + request_timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
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
                    deadline = max(deadline, time.monotonic() + sliding_window)
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
                    completed_normally = True
                    final_reply = str(event.get("reply") or "")
                    emitted_so_far = "".join(emitted_parts)

                    if not emitted_so_far and final_reply:
                        yield final_reply
                        emitted_so_far = final_reply
                    elif final_reply and final_reply.startswith(emitted_so_far):
                        tail = final_reply[len(emitted_so_far) :]
                        if tail:
                            yield tail

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
    ) -> str:
        chunks: List[str] = []
        async for delta in self.dispatch_stream_on_slot(
            slot=slot,
            prompt=prompt,
            target_model=target_model,
            reset_chat=reset_chat,
            suppress_citations=suppress_citations,
            request_timeout=request_timeout,
        ):
            if delta:
                chunks.append(delta)
        return "".join(chunks).strip()

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
            ok = await self._safe_ws_send(client, command)
            if not ok:
                self.unregister_client(client.client_id, reason="ws_send_failed")
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
                await self._safe_ws_send(client, cancel_cmd)
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

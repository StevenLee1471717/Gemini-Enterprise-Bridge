"""Browser WebSocket + HTTP-Polling Bridge Hub with FIFO serialization and live chunk streaming."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger("gemini_bridge.hub")


class BridgeUnavailableError(RuntimeError):
    """Raised when no active Gemini Enterprise browser client is connected."""


class BridgeTimeoutError(TimeoutError):
    """Raised when waiting for the queue or browser reply times out."""


class BridgeExecutionError(RuntimeError):
    """Raised when the browser driver reports an execution failure."""


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
    completed_requests: int = 0
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
            "completed_requests": self.completed_requests,
        }


@dataclass
class InFlightRequest:
    request_id: str
    client_id: str
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
    """Coordinates connected browser sessions, serializes DOM execution, and streams chunks."""

    def __init__(self, include_citations: bool = True, callback_base_url: str = "") -> None:
        self.include_citations = include_citations
        self.callback_base_url = callback_base_url.rstrip("/")
        self._clients: Dict[str, BrowserClientState] = {}
        self._pending: Dict[str, InFlightRequest] = {}
        self._execution_lock = asyncio.Lock()
        self._waiting_count = 0
        self._total_requests = 0
        self._total_errors = 0
        self._recent_logs: List[Dict[str, Any]] = []

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
        )
        self._clients[cid] = state
        if prev is not None and prev.websocket is not None and prev.websocket is not websocket:
            try:
                await prev.websocket.close(code=1000)
            except Exception:
                pass
        self.add_log("info", f"Extension WebSocket connected ({cid})", cid)
        await self._safe_ws_send(
            state,
            {
                "type": "welcome",
                "client_id": cid,
                "server_time": int(time.time()),
            },
        )
        return state

    def unregister_client(
        self,
        client_id: str,
        reason: str = "disconnected",
        websocket: Optional[WebSocket] = None,
    ) -> None:
        existing = self._clients.get(client_id)
        if existing is None:
            return
        # Guard against race where an older closing socket evicts a newer live socket with the same client_id
        if websocket is not None and existing.transport == "websocket" and existing.websocket is not websocket:
            return
        state = self._clients.pop(client_id, None)
        if state is not None:
            state.websocket = None
            self.add_log("warn", f"Browser client {client_id} disconnected ({reason})", client_id)
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
            existing.tab_url = tab_url or existing.tab_url
            existing.tab_title = tab_title or existing.tab_title
            existing.driver_mode = driver_mode or existing.driver_mode
            if active_model:
                existing.active_model = active_model
            existing.tab_ready = bool(existing.tab_url or tab_url)
            return existing

        state = BrowserClientState(
            client_id=cid,
            transport="http_poll",
            user_agent=user_agent,
            tab_url=tab_url,
            tab_title=tab_title,
            tab_ready=True,
            driver_mode=driver_mode or "auto",
            active_model=active_model,
        )
        self._clients[cid] = state
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
        """Choose the healthiest connected browser client (prefer ready WS tab, then ready HTTP tab)."""
        self._prune_stale_http_clients()
        now = time.time()
        alive = [c for c in self._clients.values() if c.is_alive(now)]
        if not alive:
            return None

        def sort_key(c: BrowserClientState) -> tuple:
            has_vertex_tab = "vertexaisearch.cloud.google.com" in (c.tab_url or "")
            return (
                1 if c.tab_ready else 0,
                1 if has_vertex_tab else 0,
                1 if c.transport == "websocket" else 0,
                c.last_seen_at,
            )

        alive.sort(key=sort_key, reverse=True)
        chosen = alive[0]
        if not chosen.tab_ready:
            return None
        return chosen

    def has_ready_client(self) -> bool:
        return self.select_active_client() is not None

    def status_snapshot(self) -> Dict[str, Any]:
        self._prune_stale_http_clients()
        now = time.time()
        clients = [c.to_public_dict() for c in self._clients.values() if c.is_alive(now)]
        active = self.select_active_client()
        return {
            "browser_connected": active is not None,
            "active_client": active.to_public_dict() if active else None,
            "clients": clients,
            "busy": self._execution_lock.locked(),
            "queued_requests": self._waiting_count,
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

        if msg_type in {"hello", "tab_status", "heartbeat"}:
            if state is not None:
                if "tab_id" in data:
                    state.tab_id = data.get("tab_id")
                if data.get("tab_url") is not None:
                    state.tab_url = str(data.get("tab_url") or "")
                if data.get("tab_title") is not None:
                    state.tab_title = str(data.get("tab_title") or "")
                if data.get("ready") is not None:
                    state.tab_ready = bool(data.get("ready"))
                if data.get("driver_mode"):
                    state.driver_mode = str(data.get("driver_mode"))
                if data.get("captured_rpc") is not None:
                    state.captured_rpc = bool(data.get("captured_rpc"))
                if data.get("active_model"):
                    state.active_model = str(data.get("active_model"))
                if data.get("user_agent"):
                    state.user_agent = str(data.get("user_agent"))
            if msg_type == "heartbeat":
                return {"type": "pong", "server_time": int(time.time())}
            return {"type": "ack", "server_time": int(time.time())}

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

        # Support both structured `request_complete` AND legacy `{request_id, reply}` payloads
        if msg_type == "request_complete" or "reply" in data:
            if data.get("chunked") and inflight.reply_chunks:
                total_c = int(data.get("total_chunks") or inflight.expected_reply_chunks or len(inflight.reply_chunks))
                reply = "".join(inflight.reply_chunks.get(i, "") for i in range(total_c))
                inflight.reply_chunks.clear()
            else:
                reply = str(data.get("reply") or "")
            citations = data.get("citations") if isinstance(data.get("citations"), list) else []
            source = str(data.get("source") or "browser")
            if state is not None:
                state.completed_requests += 1
                if "active_model" in data and data.get("active_model") is not None:
                    state.active_model = str(data.get("active_model"))
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
    # Prompt Dispatch & Streaming Pipeline
    # ------------------------------------------------------------------

    async def dispatch_stream(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 180.0,
        queue_timeout: float = 90.0,
    ) -> AsyncIterator[str]:
        """Serialize execution on the active browser session and yield text chunks in real time."""
        self._total_requests += 1
        self._waiting_count += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._execution_lock.acquire(), timeout=queue_timeout)
                acquired = True
            except asyncio.TimeoutError as exc:
                self._total_errors += 1
                raise BridgeTimeoutError(
                    f"等待浏览器队列超时（>{queue_timeout:.0f}s），前序请求仍在生成中。"
                ) from exc
        finally:
            self._waiting_count = max(0, self._waiting_count - 1)

        # Sanitize NUL/control characters that can break ProseMirror/DOM text nodes on large tool outputs
        sanitized_prompt = prompt.replace("\x00", "")
        req_id = uuid.uuid4().hex
        client: Optional[BrowserClientState] = None
        completed_normally = False

        try:
            client = self.select_active_client()
            if client is None:
                self._total_errors += 1
                raise BridgeUnavailableError(
                    "未检测到挂载的 Gemini Enterprise 浏览器页面。请打开 "
                    "https://vertexaisearch.cloud.google.com 并确保浏览器扩展或油猴脚本处于已连接状态。"
                )

            inflight = InFlightRequest(
                request_id=req_id,
                client_id=client.client_id,
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

            # Chunk oversized prompts (>32KB, e.g. large MCP tool schemas or 100KB+ tool results)
            # so WebSocket frames and browser IPC messages never hit frame-size truncation!
            if len(sanitized_prompt) > PROMPT_CHUNK_THRESHOLD:
                slices = [
                    sanitized_prompt[i : i + PROMPT_CHUNK_SIZE]
                    for i in range(0, len(sanitized_prompt), PROMPT_CHUNK_SIZE)
                ]
                total_chunks = len(slices)
                for idx, chunk_str in enumerate(slices):
                    await self._send_command_to_client(
                        client,
                        {
                            "type": "execute_prompt_chunk",
                            "request_id": req_id,
                            "chunk_index": idx,
                            "total_chunks": total_chunks,
                            "data": chunk_str,
                        },
                    )
                command = {
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
            await self._send_command_to_client(client, command)

            emitted_parts: List[str] = []
            deadline = time.monotonic() + request_timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._total_errors += 1
                    raise BridgeTimeoutError(f"网页端响应超时（>{request_timeout:.0f}s）。")

                try:
                    event = await asyncio.wait_for(
                        inflight.events.get(),
                        timeout=min(remaining, 30.0),
                    )
                except asyncio.TimeoutError:
                    # Loop to re-check overall deadline while allowing long thinking phases
                    continue

                ev_type = event.get("type")
                if ev_type == "chunk":
                    delta = event.get("delta", "")
                    if delta:
                        emitted_parts.append(delta)
                        yield delta
                    # Extend deadline slightly while chunks are actively flowing
                    deadline = max(deadline, time.monotonic() + min(request_timeout, 60.0))

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
            if not completed_normally and client is not None:
                await self._send_cancel_safely(client, req_id)
            if acquired:
                self._execution_lock.release()

    async def dispatch_complete(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        reset_chat: bool = False,
        suppress_citations: bool = False,
        request_timeout: float = 180.0,
        queue_timeout: float = 90.0,
    ) -> str:
        chunks: List[str] = []
        async for delta in self.dispatch_stream(
            prompt=prompt,
            target_model=target_model,
            reset_chat=reset_chat,
            suppress_citations=suppress_citations,
            request_timeout=request_timeout,
            queue_timeout=queue_timeout,
        ):
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

    async def _send_cancel_safely(self, client: BrowserClientState, request_id: str) -> None:
        cancel_cmd = {"type": "cancel_request", "request_id": request_id}
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

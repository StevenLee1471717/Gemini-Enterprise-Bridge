"""FastAPI OpenAI-compatible Gateway for Gemini Enterprise (Vertex AI Search / Agentspace)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from bridge_hub import (
    BridgeExecutionError,
    BridgeTimeoutError,
    BridgeUnavailableError,
    BrowserBridgeHub,
)
from config import BridgeSettings
from context_builder import ConversationContextBuilder
from dashboard_html import DASHBOARD_HTML
from extension_exporter import export_extension_bundle
from gcloud_provider import GCloudUpstreamError, GCloudVertexProvider
from schemas import (
    AVAILABLE_MODELS,
    BridgeCallbackPayload,
    BridgeEventPayload,
    BridgeRegisterRequest,
    ChatCompletionRequest,
    build_chat_completion_chunk,
    build_chat_completion_response,
    build_usage,
    estimate_tokens,
    parse_model_and_session,
    resolve_target_web_model,
)
from updater import (
    EXIT_RESTART_FOR_UPDATE,
    UpdateError,
    UpdateIntegrityError,
    UpdateManager,
)
from usage_stats import UsageTracker
from version import APP_VERSION
from tool_calling import (
    ToolStreamProcessor,
    build_sse_tool_call_deltas,
    is_tool_generation_enabled,
    normalize_request_tools,
    normalize_tool_choice,
    parse_assistant_output,
    split_text_slices,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gemini_bridge.server")


def _openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "bridge_error",
    code: Optional[str] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            },
        },
    )


def create_app(
    settings: Optional[BridgeSettings] = None,
    update_manager: Optional[UpdateManager] = None,
) -> FastAPI:
    cfg = settings or BridgeSettings.from_env()
    root_dir = Path(__file__).resolve().parent
    updater = update_manager or UpdateManager(
        root_dir=root_dir,
        current_version=APP_VERSION,
        repo=cfg.github_repo,
    )
    cb_host = "127.0.0.1" if cfg.host in {"0.0.0.0", "::"} else cfg.host
    hub = BrowserBridgeHub(
        include_citations=cfg.include_citations,
        callback_base_url=f"http://{cb_host}:{cfg.port}",
        max_parallel_tabs=cfg.max_parallel_tabs,
        auto_spawn_tabs=cfg.auto_spawn_tabs,
        session_affinity_ttl_seconds=cfg.session_affinity_ttl_seconds,
        backend_version=updater.current_version,
    )
    context_builder = ConversationContextBuilder(
        default_mode=cfg.context_mode,
        auto_reset_new_chat=cfg.auto_reset_new_chat,
    )
    usage_tracker = UsageTracker(
        storage_path=cfg.usage_stats_path or None,
        usd_to_cny_rate=cfg.usd_to_cny_rate,
    )
    gcloud = GCloudVertexProvider(cfg)

    def _resolve_active_model_hint(hub_snap: Optional[Dict[str, Any]] = None) -> str:
        snap = hub_snap if hub_snap is not None else hub.status_snapshot()
        active_client = snap.get("active_client")
        if isinstance(active_client, dict) and active_client.get("active_model"):
            return str(active_client["active_model"])
        for ws_slot in snap.get("worker_slots") or []:
            if isinstance(ws_slot, dict) and ws_slot.get("active_model"):
                return str(ws_slot["active_model"])
        return ""

    def build_live_usage_snapshot(include_catalog: bool = False) -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        ctx_monitor = context_builder.get_context_snapshot(hub_snap.get("worker_slots") or [])
        model_hint = _resolve_active_model_hint(hub_snap) or str(
            (ctx_monitor.get("primary") or {}).get("active_model") or ""
        )
        usage_snap = usage_tracker.snapshot(
            active_model_hint=model_hint,
            include_catalog=include_catalog,
        )
        return {
            "usage": usage_snap,
            "context_monitor": ctx_monitor,
        }

    def _sync_extension_for_reload(_old_ext_ver: str) -> None:
        if cfg.auto_export_extension:
            try:
                export_extension_bundle(updater.root_dir)
            except Exception:
                pass

    hub.usage_snapshot_provider = lambda: build_live_usage_snapshot(include_catalog=False)
    hub.on_slot_removed = context_builder.drop_slot
    hub.on_slot_migrated = context_builder.migrate_slot_key
    hub.on_extension_version_reported = updater.record_extension_version
    hub.on_outdated_extension_detected = _sync_extension_for_reload

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if cfg.auto_export_extension:
            try:
                ext_path = export_extension_bundle(updater.root_dir)
                logger.info("Chrome/Edge MV3 扩展目录已就绪 (v%s): %s", updater.current_version, ext_path)
            except Exception as exc:  # pragma: no cover
                logger.warning("自动导出 extension 目录失败（不影响主目录加载）: %s", exc)

        bg_check_task: Optional[asyncio.Task[Any]] = None
        if cfg.auto_update:
            async def _initial_github_probe() -> None:
                try:
                    snap = await asyncio.to_thread(updater.check_github_update, False, 6.0)
                    if snap.get("backend_outdated"):
                        logger.warning(
                            "发现 GitHub 新版本 v%s（当前本地后端 v%s），可在控制台或插件弹窗一键自动更新！",
                            snap.get("latest_github_version"),
                            updater.current_version,
                        )
                except Exception:
                    pass

            bg_check_task = asyncio.create_task(_initial_github_probe())

        logger.info(
            "Gemini Enterprise Bridge v%s 已启动: http://%s:%d/ (OpenAI Base URL: http://%s:%d/v1)",
            updater.current_version,
            cfg.host,
            cfg.port,
            cfg.host,
            cfg.port,
        )
        yield
        if bg_check_task is not None and not bg_check_task.done():
            bg_check_task.cancel()

    app = FastAPI(
        title="Gemini Enterprise Web-to-API Bridge",
        version=updater.current_version,
        description="Browser-in-the-loop OpenAI-compatible proxy for Google Cloud Vertex AI Search / Agentspace",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.hub = hub
    app.state.context_builder = context_builder
    app.state.usage_tracker = usage_tracker
    app.state.update_manager = updater
    app.state.gcloud = gcloud

    class _WebSocketSafeCORSMiddleware:
        """Apply CORSMiddleware to HTTP requests while letting WebSocket handshakes pass cleanly."""

        def __init__(self, asgi_app: Any, **cors_kwargs: Any) -> None:
            self.asgi_app = asgi_app
            self.cors_app = CORSMiddleware(asgi_app, **cors_kwargs)

        async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "websocket":
                await self.asgi_app(scope, receive, send)
                return
            await self.cors_app(scope, receive, send)

    wildcard_cors = "*" in (cfg.cors_allow_origins or [])
    app.add_middleware(
        _WebSocketSafeCORSMiddleware,
        allow_origins=[] if wildcard_cors else cfg.cors_allow_origins,
        allow_origin_regex=r".*" if wildcard_cors else None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def private_network_access_middleware(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    async def verify_api_key(
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> None:
        if not cfg.api_key:
            return
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        elif x_api_key:
            token = x_api_key.strip()
        if token != cfg.api_key:
            raise PermissionError("Invalid API key")

    @app.exception_handler(PermissionError)
    async def permission_exception_handler(_req: Request, _exc: PermissionError) -> JSONResponse:
        return _openai_error_response(
            status_code=401,
            message="API Key 校验未通过，请检查客户端 Authorization Bearer 配置。",
            error_type="invalid_request_error",
            code="invalid_api_key",
        )

    # ------------------------------------------------------------------
    # Dashboard & Health Endpoints
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root_dashboard() -> HTMLResponse:
        return HTMLResponse(content=DASHBOARD_HTML)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": updater.current_version,
            "browser_connected": hub.has_ready_client(),
            "gcloud_configured": gcloud.is_configured(),
            "backend_mode": cfg.backend_mode,
        }

    @app.get("/api/status")
    async def api_status() -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        if hub_snap.get("connected_extension_version"):
            updater.record_extension_version(str(hub_snap["connected_extension_version"]))
        upd_snap = updater.status_snapshot(include_remote_if_cached=True)
        ctx_monitor = context_builder.get_context_snapshot(hub_snap.get("worker_slots") or [])
        model_hint = _resolve_active_model_hint(hub_snap) or str(
            (ctx_monitor.get("primary") or {}).get("active_model") or ""
        )
        return {
            "status": "ok",
            "version": updater.current_version,
            "backend_version": updater.current_version,
            "connected_extension_version": upd_snap["connected_extension_version"],
            "backend_update_required": bool(
                upd_snap["backend_update_required"] or hub_snap.get("backend_update_required")
            ),
            "backend_mode": cfg.backend_mode,
            "context_mode": cfg.context_mode,
            "gcloud_configured": gcloud.is_configured(),
            "hub": hub_snap,
            "update": upd_snap,
            "usage": usage_tracker.snapshot(active_model_hint=model_hint, include_catalog=False),
            "context_monitor": ctx_monitor,
        }

    @app.get("/api/update/status")
    async def api_update_status(force: bool = False) -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        if hub_snap.get("connected_extension_version"):
            updater.record_extension_version(str(hub_snap["connected_extension_version"]))
        info = await asyncio.to_thread(updater.check_github_update, force)
        return {
            "status": "ok",
            **info,
        }

    @app.post("/api/update/extension")
    async def api_update_extension() -> JSONResponse:
        try:
            result = await asyncio.to_thread(updater.update_extension_only)
        except UpdateIntegrityError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": str(exc), "code": "integrity_error"},
            )
        except UpdateError as exc:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(exc), "code": "update_failed"},
            )
        updated_ver = str(result.get("updated_version") or updater.current_version)
        await hub.broadcast_ws_event(
            {"type": "extension_reload_available", "version": updated_ver},
            required_capability="auto_update",
        )
        return JSONResponse(status_code=200, content=result)

    @app.post("/api/update/apply")
    async def api_update_apply(request: Request) -> JSONResponse:
        supervised = os.getenv("BRIDGE_SUPERVISED", "").strip() == "1"
        requested_restart: Optional[bool] = None
        try:
            body = await request.json()
            if isinstance(body, dict) and "restart" in body:
                requested_restart = bool(body["restart"])
        except Exception:
            pass

        try:
            result = await asyncio.to_thread(updater.update_backend_and_extension)
        except UpdateIntegrityError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": str(exc), "code": "integrity_error"},
            )
        except UpdateError as exc:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(exc), "code": "update_failed"},
            )

        updated_ver = str(result.get("updated_version") or updater.current_version)
        await hub.broadcast_ws_event(
            {"type": "extension_reload_available", "version": updated_ver},
            required_capability="auto_update",
        )

        will_restart = supervised if requested_restart is None else (requested_restart and supervised)
        if will_restart:
            async def _delayed_hot_exit() -> None:
                await asyncio.sleep(0.35)
                os._exit(EXIT_RESTART_FOR_UPDATE)

            asyncio.create_task(_delayed_hot_exit())
            msg = f"已下载并写入 v{updated_ver} 更新文件，本地网关正在自动热重启（约 2~3 秒恢复）..."
        else:
            msg = f"已下载并写入 v{updated_ver} 更新文件；当前为手动启动模式，请重启 server.py（使用 start.ps1 启动可自动热重启）。"

        return JSONResponse(
            status_code=200,
            content={
                **result,
                "supervised": supervised,
                "restart_scheduled": will_restart,
                "exit_code": EXIT_RESTART_FOR_UPDATE if will_restart else None,
                "message": msg,
            },
        )

    @app.get("/api/usage")
    async def api_usage() -> Dict[str, Any]:
        live = build_live_usage_snapshot(include_catalog=True)
        return {
            "status": "ok",
            "usage": live["usage"],
            "context_monitor": live["context_monitor"],
        }

    @app.post("/api/usage/reset")
    async def api_usage_reset(request: Request, scope: Optional[str] = None) -> Dict[str, Any]:
        resolved_scope = scope or ""
        if not resolved_scope:
            try:
                body = await request.json()
                if isinstance(body, dict) and body.get("scope"):
                    resolved_scope = str(body["scope"])
            except Exception:
                pass
        resolved_scope = (resolved_scope or "all").strip().lower()
        if resolved_scope not in {"session", "today", "all"}:
            resolved_scope = "all"
        usage_tracker.reset(scope=resolved_scope)
        live = build_live_usage_snapshot(include_catalog=True)
        await hub.broadcast_ws_event({"type": "usage_update", "usage_snapshot": live})
        return {
            "ok": True,
            "scope": resolved_scope,
            "usage": live["usage"],
            "context_monitor": live["context_monitor"],
        }

    # ------------------------------------------------------------------
    # Browser Extension WebSocket Endpoint
    # ------------------------------------------------------------------

    @app.websocket("/ws/browser")
    async def websocket_browser_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        query_cid = websocket.query_params.get("client_id")
        client = await hub.register_websocket(websocket, client_id=query_cid)
        logger.info("浏览器扩展前端已挂载 (%s)", client.client_id)
        try:
            while True:
                data = await websocket.receive_json()
                if isinstance(data, dict):
                    reply_packet = await hub.handle_browser_event(client.client_id, data)
                    if reply_packet is not None:
                        reload_ver = reply_packet.pop("_emit_extension_reload", None)
                        await websocket.send_json(reply_packet)
                        if reload_ver:
                            await websocket.send_json(
                                {
                                    "type": "extension_reload_available",
                                    "version": str(reload_ver),
                                }
                            )
        except WebSocketDisconnect:
            logger.info("浏览器扩展前端已断开连接 (%s)", client.client_id)
        except Exception as exc:
            logger.warning("WebSocket 连接异常结束 (%s): %s", client.client_id, exc)
        finally:
            hub.unregister_client(client.client_id, reason="ws_closed", websocket=websocket)

    # ------------------------------------------------------------------
    # Tampermonkey HTTP Long-Poll Fallback Bridge Endpoints (CSP-immune)
    # ------------------------------------------------------------------

    @app.post("/api/bridge/register")
    async def register_http_bridge(payload: BridgeRegisterRequest) -> Dict[str, Any]:
        state = hub.register_http_client(
            client_id=payload.client_id,
            user_agent=payload.user_agent or "",
            tab_id=payload.tab_id,
            tab_url=payload.tab_url or "",
            tab_title=payload.tab_title or "",
            driver_mode=payload.driver_mode,
            active_model=payload.active_model or "",
        )
        return {"ok": True, "client_id": state.client_id}

    @app.get("/api/bridge/poll")
    async def poll_http_bridge(client_id: str, timeout: float = 18.0) -> Dict[str, Any]:
        bounded = max(1.0, min(timeout, 25.0))
        command = await hub.poll_http_command(client_id=client_id, timeout=bounded)
        return {
            "ok": True,
            "command": command,
            "usage_snapshot": build_live_usage_snapshot(include_catalog=False),
        }

    @app.post("/api/bridge/event")
    async def receive_http_bridge_event(payload: BridgeEventPayload) -> Dict[str, Any]:
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        raw["type"] = payload.event
        ack = await hub.handle_browser_event(payload.client_id, raw)
        return {
            "ok": True,
            "ack": ack,
            "usage_snapshot": build_live_usage_snapshot(include_catalog=False),
        }

    @app.post("/api/bridge/callback")
    async def receive_async_bridge_callback(payload: BridgeCallbackPayload) -> Dict[str, Any]:
        """Unlimited-body HTTP callback endpoint for 100KB+ tool calls & long completions."""
        cid = payload.client_id or ""
        if not cid:
            active = hub.select_active_client()
            cid = active.client_id if active else "http_callback"
        if payload.error:
            await hub.handle_browser_event(
                cid,
                {
                    "type": "request_error",
                    "request_id": payload.request_id,
                    "error": payload.error,
                },
            )
        else:
            await hub.handle_browser_event(
                cid,
                {
                    "type": "request_complete",
                    "request_id": payload.request_id,
                    "tab_id": payload.tab_id,
                    "reply": payload.reply or "",
                    "citations": payload.citations or [],
                    "source": payload.source or "http_callback",
                    "active_model": payload.active_model,
                },
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Agentic Loop & Tool Calling Support
    # ------------------------------------------------------------------

    @app.post("/v1/chat/completions/tools", dependencies=[Depends(verify_api_key)])
    async def chat_tools(
        request: ChatCompletionRequest,
        x_session_id: Optional[str] = Header(default=None),
        x_bridge_session: Optional[str] = Header(default=None),
    ) -> Any:
        return await chat_completions(
            request=request,
            x_session_id=x_session_id,
            x_bridge_session=x_bridge_session,
        )

    # ------------------------------------------------------------------
    # OpenAI-Compatible /v1/models & /v1/chat/completions
    # ------------------------------------------------------------------

    @app.get("/v1/models", dependencies=[Depends(verify_api_key)])
    @app.get("/models", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def list_models() -> Dict[str, Any]:
        return {"object": "list", "data": AVAILABLE_MODELS}

    def _select_upstream(model_name: str) -> str:
        """Decide between 'browser' and 'gcloud' upstream."""
        lowered = (model_name or "").strip().lower()
        if lowered == "gemini-enterprise-gcloud" or cfg.backend_mode == "gcloud":
            return "gcloud"
        if cfg.backend_mode == "auto":
            if hub.has_ready_client():
                return "browser"
            if gcloud.is_configured():
                return "gcloud"
        return "browser"

    @app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
    @app.post("/chat/completions", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def chat_completions(
        request: ChatCompletionRequest,
        x_session_id: Optional[str] = Header(default=None),
        x_bridge_session: Optional[str] = Header(default=None),
    ) -> Any:
        if not request.messages:
            return _openai_error_response(
                status_code=400,
                message="messages 不能为空。",
                error_type="invalid_request_error",
                code="empty_messages",
            )

        raw_model_name = request.model or cfg.default_model
        base_model_name, model_session_tag = parse_model_and_session(raw_model_name)
        model_name = base_model_name or cfg.default_model
        explicit_session_id = (
            model_session_tag
            or (x_session_id.strip() if x_session_id and x_session_id.strip() else None)
            or (x_bridge_session.strip() if x_bridge_session and x_bridge_session.strip() else None)
            or (request.session_id.strip() if request.session_id and request.session_id.strip() else None)
        )

        upstream = _select_upstream(model_name)
        target_web_model = resolve_target_web_model(model_name)
        completion_id = uuid.uuid4().hex
        created_ts = int(time.time())
        req_started_mono = time.monotonic()
        include_usage = bool(request.stream_options and request.stream_options.include_usage)

        norm_tools = normalize_request_tools(request.tools, request.functions)
        norm_tool_choice = normalize_tool_choice(request.tool_choice, request.function_call)
        tools_active = is_tool_generation_enabled(norm_tools, norm_tool_choice)
        json_mode_active = isinstance(request.response_format, dict) and str(
            request.response_format.get("type") or ""
        ).lower() in {"json_object", "json_schema"}
        suppress_citations = tools_active or json_mode_active

        # ---------------- Option 1: Official GCloud Upstream ----------------
        prompt_for_usage = "\n".join(m.text_content() for m in request.messages)

        if upstream == "gcloud":
            if not gcloud.is_configured():
                return _openai_error_response(
                    status_code=503,
                    message="未配置 GCP_PROJECT_ID，无法使用 gemini-enterprise-gcloud 模式。请在 .env 中配置或改用 gemini-enterprise-web。",
                    error_type="service_unavailable_error",
                    code="gcloud_not_configured",
                )
            max_out = request.max_completion_tokens or request.max_tokens

            if not request.stream:
                try:
                    raw_reply = await gcloud.complete_chat(
                        request.messages,
                        temperature=request.temperature,
                        max_tokens=max_out,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )
                except GCloudUpstreamError as exc:
                    return _openai_error_response(502, str(exc), "upstream_error", "gcloud_failed")

                parsed = parse_assistant_output(
                    raw_reply,
                    tools=norm_tools,
                    tool_choice=norm_tool_choice,
                    parallel_tool_calls=request.parallel_tool_calls,
                    response_format=request.response_format,
                )
                resp_obj = build_chat_completion_response(
                    request_id=completion_id,
                    model=model_name,
                    reply_text=parsed.content,
                    prompt_text=prompt_for_usage,
                    created=created_ts,
                    tool_calls=parsed.tool_calls or None,
                    finish_reason=parsed.finish_reason,
                )
                u_info = resp_obj.get("usage") or {}
                usage_tracker.record_request(
                    requested_model=model_name,
                    prompt_tokens=int(u_info.get("prompt_tokens") or estimate_tokens(prompt_for_usage)),
                    completion_tokens=int(u_info.get("completion_tokens") or 0),
                    gcloud_fallback_model=cfg.gcloud_model,
                    strategy="gcloud_direct",
                    slot_key="gcloud",
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                await hub.broadcast_ws_event(
                    {"type": "usage_update", "usage_snapshot": build_live_usage_snapshot()}
                )
                return resp_obj

            async def gcloud_sse() -> AsyncIterator[str]:
                initial = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=model_name,
                    delta={"role": "assistant", "content": ""},
                    created=created_ts,
                )
                yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
                processor = ToolStreamProcessor(
                    tools=norm_tools,
                    tool_choice=norm_tool_choice,
                    parallel_tool_calls=request.parallel_tool_calls,
                    response_format=request.response_format,
                )
                finish_reason = "stop"
                completion_usage_str = ""
                try:
                    async for delta in gcloud.stream_chat(
                        request.messages,
                        temperature=request.temperature,
                        max_tokens=max_out,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    ):
                        for safe_piece in processor.feed(delta):
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    tail_pieces, parsed = processor.finalize()
                    for raw_piece in tail_pieces:
                        for safe_piece in split_text_slices(raw_piece):
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    if parsed.has_tool_calls:
                        finish_reason = "tool_calls"
                        for tc_batch in build_sse_tool_call_deltas(parsed.tool_calls):
                            tc_chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=model_name,
                                delta={"tool_calls": tc_batch},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"
                        completion_usage_str = json.dumps(parsed.tool_calls, ensure_ascii=False)
                    else:
                        completion_usage_str = parsed.content or ""
                    gcloud_u = build_usage(prompt_for_usage, completion_usage_str)
                    usage_tracker.record_request(
                        requested_model=model_name,
                        prompt_tokens=gcloud_u["prompt_tokens"],
                        completion_tokens=gcloud_u["completion_tokens"],
                        gcloud_fallback_model=cfg.gcloud_model,
                        strategy="gcloud_stream",
                        slot_key="gcloud",
                        duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                    )
                    await hub.broadcast_ws_event(
                        {"type": "usage_update", "usage_snapshot": build_live_usage_snapshot()}
                    )
                except GCloudUpstreamError as exc:
                    err_chunk = build_chat_completion_chunk(
                        request_id=completion_id,
                        model=model_name,
                        delta={"content": f"\n\n[GCloud Upstream Error: {exc}]"},
                        created=created_ts,
                    )
                    yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"

                stop_chunk = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=model_name,
                    delta={},
                    finish_reason=finish_reason,
                    created=created_ts,
                )
                yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                if include_usage:
                    usage_chunk = build_chat_completion_chunk(
                        request_id=completion_id,
                        model=model_name,
                        delta={},
                        created=created_ts,
                        usage=build_usage(prompt_for_usage, completion_usage_str),
                    )
                    yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                gcloud_sse(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # ---------------- Option 2: Parallel Browser-in-the-loop Worker Pool ----------------
        if not hub.has_ready_client():
            return _openai_error_response(
                status_code=503,
                message=(
                    "未检测到挂载的 Gemini Enterprise 浏览器页面。请确保已在 Chrome/Edge 打开并登录 "
                    "https://vertexaisearch.cloud.google.com ，且已启用 Gemini Enterprise Bridge 扩展或油猴脚本。"
                ),
                error_type="bridge_unavailable_error",
                code="browser_not_connected",
            )

        if not request.stream:
            matching_slots = context_builder.find_matching_slots(
                hub.list_ready_slot_keys(),
                request.messages,
                model=model_name,
            )
            aff_slot = hub.get_affinity_slot_key(explicit_session_id)
            ephemeral_aux = bool(
                not tools_active
                and (
                    (aff_slot and context_builder.has_active_tool_session(aff_slot))
                    or any(context_builder.has_active_tool_session(k) for k in matching_slots)
                )
            )
            if ephemeral_aux:
                matching_slots = [
                    k for k in matching_slots if not context_builder.has_active_tool_session(k)
                ]
            try:
                async with hub.acquire_slot(
                    explicit_session_id=explicit_session_id,
                    matching_slot_keys=matching_slots,
                    queue_timeout=cfg.queue_timeout_seconds,
                    ephemeral_aux=ephemeral_aux,
                ) as slot:
                    session_key = slot.slot_key
                    prepared = context_builder.build(
                        messages=request.messages,
                        model=model_name,
                        session_key=session_key,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )
                    raw_reply = await hub.dispatch_complete_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                    )
                    parsed = parse_assistant_output(
                        raw_reply,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )
                    slot.pending_tool_call_until = (
                        time.time() + 60.0 if parsed.has_tool_calls else 0.0
                    )
                    effective_slot_model = slot.active_model or target_web_model
                    context_builder.record_completed_turn(
                        session_key=session_key,
                        sent_history=prepared.normalized_history,
                        assistant_reply=parsed.content or "",
                        assistant_tool_calls=parsed.tool_calls or None,
                        active_model=effective_slot_model,
                        tools_fingerprint=prepared.tools_fingerprint,
                        system_fingerprint=prepared.system_fingerprint,
                        system_tokens=prepared.system_tokens,
                        tools_tokens=prepared.tools_tokens,
                        last_delta_tokens=prepared.delta_prompt_tokens,
                        last_strategy=prepared.strategy_used,
                    )
                    comp_text_for_stats = parsed.content or ""
                    if parsed.has_tool_calls:
                        comp_text_for_stats += " " + json.dumps(
                            parsed.tool_calls, ensure_ascii=False
                        )
                    slot_ctx = context_builder.get_slot_context_metrics(session_key) or {}
                    usage_tracker.record_request(
                        requested_model=model_name,
                        prompt_tokens=max(
                            prepared.logical_prompt_tokens,
                            estimate_tokens(prompt_for_usage),
                        ),
                        completion_tokens=estimate_tokens(comp_text_for_stats),
                        browser_active_model=effective_slot_model,
                        strategy=prepared.strategy_used,
                        cached_tokens=prepared.cached_prefix_tokens,
                        delta_prompt_tokens=prepared.delta_prompt_tokens,
                        slot_key=session_key,
                        tab_id=slot.tab_id,
                        context_tokens=slot_ctx.get("total_context_tokens"),
                        context_limit=slot_ctx.get("context_limit_tokens"),
                        duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                    )
                    slot_header_val = slot.slot_key
            except BridgeUnavailableError as exc:
                return _openai_error_response(503, str(exc), "bridge_unavailable_error", "browser_not_connected")
            except BridgeTimeoutError as exc:
                return _openai_error_response(504, str(exc), "timeout_error", "browser_timeout")
            except BridgeExecutionError as exc:
                return _openai_error_response(502, str(exc), "bridge_execution_error", "dom_execution_failed")

            resp_payload = build_chat_completion_response(
                request_id=completion_id,
                model=raw_model_name,
                reply_text=parsed.content,
                prompt_text=prompt_for_usage,
                created=created_ts,
                tool_calls=parsed.tool_calls or None,
                finish_reason=parsed.finish_reason,
            )
            await hub.broadcast_ws_event(
                {"type": "usage_update", "usage_snapshot": build_live_usage_snapshot()}
            )
            resp_headers = {"X-Bridge-Slot": slot_header_val}
            if explicit_session_id:
                resp_headers["X-Bridge-Session-Id"] = explicit_session_id
            return JSONResponse(content=resp_payload, headers=resp_headers)

        async def browser_sse() -> AsyncIterator[str]:
            initial = build_chat_completion_chunk(
                request_id=completion_id,
                model=raw_model_name,
                delta={"role": "assistant", "content": ""},
                created=created_ts,
            )
            yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"

            processor = ToolStreamProcessor(
                tools=norm_tools,
                tool_choice=norm_tool_choice,
                parallel_tool_calls=request.parallel_tool_calls,
                response_format=request.response_format,
            )
            finish_reason = "stop"
            completion_usage_str = ""
            prompt_for_usage_str = prompt_for_usage
            last_sse_ts = time.monotonic()

            try:
                matching_slots = context_builder.find_matching_slots(
                    hub.list_ready_slot_keys(),
                    request.messages,
                    model=model_name,
                )
                aff_slot = hub.get_affinity_slot_key(explicit_session_id)
                ephemeral_aux = bool(
                    not tools_active
                    and (
                        (aff_slot and context_builder.has_active_tool_session(aff_slot))
                        or any(context_builder.has_active_tool_session(k) for k in matching_slots)
                    )
                )
                if ephemeral_aux:
                    matching_slots = [
                        k for k in matching_slots if not context_builder.has_active_tool_session(k)
                    ]
                async with hub.acquire_slot(
                    explicit_session_id=explicit_session_id,
                    matching_slot_keys=matching_slots,
                    queue_timeout=cfg.queue_timeout_seconds,
                    ephemeral_aux=ephemeral_aux,
                ) as slot:
                    session_key = slot.slot_key
                    prepared = context_builder.build(
                        messages=request.messages,
                        model=model_name,
                        session_key=session_key,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )

                    async for delta in hub.dispatch_stream_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                    ):
                        if not delta:
                            if time.monotonic() - last_sse_ts >= 14.5:
                                yield ": keepalive\n\n"
                                last_sse_ts = time.monotonic()
                            continue
                        for safe_piece in processor.feed(delta):
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=raw_model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            last_sse_ts = time.monotonic()

                    tail_pieces, parsed = processor.finalize()
                    for raw_piece in tail_pieces:
                        for safe_piece in split_text_slices(raw_piece):
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=raw_model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    slot.pending_tool_call_until = (
                        time.time() + 60.0 if parsed.has_tool_calls else 0.0
                    )
                    effective_slot_model = slot.active_model or target_web_model
                    turn_recorded = False
                    if parsed.has_tool_calls:
                        finish_reason = "tool_calls"
                        for tc_batch in build_sse_tool_call_deltas(parsed.tool_calls):
                            tc_chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=raw_model_name,
                                delta={"tool_calls": tc_batch},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"
                        completion_usage_str = (parsed.content or "") + " " + json.dumps(
                            parsed.tool_calls, ensure_ascii=False
                        )
                        context_builder.record_completed_turn(
                            session_key=session_key,
                            sent_history=prepared.normalized_history,
                            assistant_reply=parsed.content or "",
                            assistant_tool_calls=parsed.tool_calls,
                            active_model=effective_slot_model,
                            tools_fingerprint=prepared.tools_fingerprint,
                            system_fingerprint=prepared.system_fingerprint,
                            system_tokens=prepared.system_tokens,
                            tools_tokens=prepared.tools_tokens,
                            last_delta_tokens=prepared.delta_prompt_tokens,
                            last_strategy=prepared.strategy_used,
                        )
                        turn_recorded = True
                    else:
                        full_reply = (parsed.content or "").strip()
                        completion_usage_str = full_reply
                        if full_reply:
                            context_builder.record_completed_turn(
                                session_key=session_key,
                                sent_history=prepared.normalized_history,
                                assistant_reply=full_reply,
                                active_model=effective_slot_model,
                                tools_fingerprint=prepared.tools_fingerprint,
                                system_fingerprint=prepared.system_fingerprint,
                                system_tokens=prepared.system_tokens,
                                tools_tokens=prepared.tools_tokens,
                                last_delta_tokens=prepared.delta_prompt_tokens,
                                last_strategy=prepared.strategy_used,
                            )
                            turn_recorded = True
                    if turn_recorded:
                        slot_ctx = context_builder.get_slot_context_metrics(session_key) or {}
                        usage_tracker.record_request(
                            requested_model=model_name,
                            prompt_tokens=max(
                                prepared.logical_prompt_tokens,
                                estimate_tokens(prompt_for_usage_str),
                            ),
                            completion_tokens=estimate_tokens(completion_usage_str),
                            browser_active_model=effective_slot_model,
                            strategy=prepared.strategy_used,
                            cached_tokens=prepared.cached_prefix_tokens,
                            delta_prompt_tokens=prepared.delta_prompt_tokens,
                            slot_key=session_key,
                            tab_id=slot.tab_id,
                            context_tokens=slot_ctx.get("total_context_tokens"),
                            context_limit=slot_ctx.get("context_limit_tokens"),
                            duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                        )
                        await hub.broadcast_ws_event(
                            {"type": "usage_update", "usage_snapshot": build_live_usage_snapshot()}
                        )
            except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError) as exc:
                err_chunk = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=raw_model_name,
                    delta={"content": f"\n\n[Bridge Error: {exc}]"},
                    created=created_ts,
                )
                yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"

            stop_chunk = build_chat_completion_chunk(
                request_id=completion_id,
                model=raw_model_name,
                delta={},
                finish_reason=finish_reason,
                created=created_ts,
            )
            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"

            if include_usage:
                usage_chunk = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=raw_model_name,
                    delta={},
                    created=created_ts,
                    usage=build_usage(prompt_for_usage_str, completion_usage_str),
                )
                yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"

        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        if explicit_session_id:
            sse_headers["X-Bridge-Session-Id"] = explicit_session_id

        return StreamingResponse(
            browser_sse(),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    runtime_cfg = BridgeSettings.from_env()
    uvicorn.run(
        app,
        host=runtime_cfg.host,
        port=runtime_cfg.port,
        ws_max_size=16 * 1024 * 1024,
        h11_max_incomplete_event_size=16 * 1024 * 1024,
    )

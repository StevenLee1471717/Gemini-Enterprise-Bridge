"""FastAPI OpenAI-compatible Gateway for Gemini Enterprise (Vertex AI Search / Agentspace)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from pydantic import ValidationError

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from bridge_hub import (
    BridgeExecutionError,
    BridgeTimeoutError,
    BridgeUnavailableError,
    BrowserBridgeHub,
)
from config import BridgeSettings
from agent_policy import (
    CARBON_AUTO_COMPACT_TOKENS,
    CARBON_CONTEXT_WINDOW,
    CODING_WORKFLOW,
    COMPACTION_INSTRUCTIONS,
    has_coding_tools,
    reading_diagnostics,
    split_checkpoint_history,
)
from context_builder import ConversationContextBuilder
from dashboard_html import DASHBOARD_HTML
from extension_exporter import export_extension_bundle
from gcloud_provider import GCloudUpstreamError, GCloudVertexProvider
from multimodal import estimate_attachment_tokens
from schemas import (
    AVAILABLE_MODELS,
    BridgeCallbackPayload,
    BridgeEventPayload,
    BridgeRegisterRequest,
    ChatCompletionRequest,
    ChatMessage,
    build_chat_completion_chunk,
    build_chat_completion_response,
    build_usage,
    estimate_tokens,
    parse_model_and_session,
    resolve_model_metadata,
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
from image_outputs import generate_images
from media_inputs import (
    FileStore,
    InvalidAttachmentError,
    collect_attachments,
    compact_messages_for_store,
    decode_base64_payload,
    parse_multipart_form,
    resolve_mime_and_name,
)
from media_outputs import format_artifacts_markdown, generate_videos, persist_browser_outputs
from responses_api import ResponseStore, ResponsesError, prepare_request, translate_response
from streaming_utils import with_keepalive
from tool_calling import (
    TOOL_OUTPUT_REPAIR_PROMPT,
    ToolProtocolError,
    ToolStreamProcessor,
    build_response_format_instructions,
    build_sse_tool_call_deltas,
    build_tool_system_instructions,
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
    context_diagnostics: Dict[str, Any] = {}

    def prompt_for_usage(request: ChatCompletionRequest) -> str:
        system_text, dialogue = context_builder._normalize_messages(request.messages)
        sections = [
            system_text,
            *(text for _, text in dialogue),
            build_tool_system_instructions(request.tools or [], request.tool_choice, request.parallel_tool_calls),
            build_response_format_instructions(request.response_format),
        ]
        if (
            has_coding_tools(request.tools)
            and is_tool_generation_enabled(request.tools or [], request.tool_choice)
            and CODING_WORKFLOW.strip() not in system_text
        ):
            sections.append(CODING_WORKFLOW)
        return "\n\n".join(filter(None, sections))

    def record_context(request: ChatCompletionRequest, prepared: Any) -> None:
        context_diagnostics.clear()
        context_diagnostics.update(
            model=request.model or cfg.default_model,
            strategy=prepared.strategy_used,
            reset_chat=prepared.reset_chat,
            web_prompt_characters=len(prepared.prompt),
            estimated_logical_input_tokens=estimate_tokens(prompt_for_usage(request)),
            **reading_diagnostics(request.messages),
        )

    @asynccontextmanager
    async def conversation_slot(**options: Any) -> AsyncIterator[Any]:
        async with hub.acquire_slot(**options) as slot:
            try:
                yield slot
            except BaseException:
                context_builder.clear_session(slot.slot_key)
                slot.pending_tool_call_until = 0.0
                raise

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
        try:
            yield
        finally:
            if bg_check_task is not None and not bg_check_task.done():
                bg_check_task.cancel()
            response_store.close()

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
    image_output_dir = Path(cfg.generated_dir) if cfg.generated_dir else (Path(__file__).resolve().parent / "generated")
    file_store = FileStore(image_output_dir)
    response_store = ResponseStore(
        file_store=file_store,
        directory=cfg.response_store_dir,
        ttl_seconds=cfg.response_store_ttl_seconds,
    )
    app.state.file_store = file_store
    app.state.response_store = response_store
    callback_origin = f"http://{cb_host}:{cfg.port}"

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
            "context": {
                "carbon_model_capacity": CARBON_CONTEXT_WINDOW,
                "carbon_checkpoint_budget": CARBON_AUTO_COMPACT_TOKENS,
                "checkpoint_policy": "Gemini CLI 50%; Enterprise web threshold unpublished",
                "token_count_source": "local_estimate_not_google_usage",
                "last_request": dict(context_diagnostics),
            },
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
                    "output_images": payload.output_images or [],
                    "output_files": payload.output_files or [],
                    "citations": payload.citations or [],
                    "source": payload.source or "http_callback",
                    "active_model": payload.active_model,
                },
            )
        return {"ok": True}

    @app.get("/api/bridge/attachments/{attachment_id}")
    async def download_staged_bridge_attachment(
        attachment_id: str,
        token: str = "",
    ) -> Response:
        """Token-guarded or file_id-staged local binary attachment endpoint."""
        if attachment_id.startswith("file_") and not token:
            try:
                meta = file_store.get_meta(attachment_id)
            except FileNotFoundError:
                return _openai_error_response(404, "附件缓存不存在或已过期。", "invalid_request_error", "file_not_found")
            return FileResponse(meta["path"], media_type=meta["mime_type"], filename=meta["filename"])
        staged, status_reason = hub.verify_staged_attachment(attachment_id, token)
        if status_reason == "forbidden":
            return _openai_error_response(
                status_code=403,
                message="附件访问令牌无效或缺失。",
                error_type="permission_error",
                code="invalid_attachment_token",
            )
        if staged is None:
            return _openai_error_response(
                status_code=404,
                message="附件不存在或已过期。",
                error_type="not_found_error",
                code="attachment_not_found",
            )
        ascii_fallback = staged.filename.encode("ascii", "ignore").decode("ascii") or "attachment"
        ascii_fallback = ascii_fallback.replace('"', "").replace("\\", "_")
        encoded_utf8 = urllib.parse.quote(staged.filename, safe="")
        headers = {
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_utf8}'
            ),
        }
        return Response(
            content=staged.data,
            media_type=staged.mime_type or "application/octet-stream",
            headers=headers,
        )

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

    @app.get("/v1/models/{model_id:path}", dependencies=[Depends(verify_api_key)])
    @app.get(
        "/models/{model_id:path}",
        dependencies=[Depends(verify_api_key)],
        include_in_schema=False,
    )
    async def retrieve_model(model_id: str) -> Any:
        clean_id = (model_id or "").strip().strip("/")
        if not clean_id:
            return {"object": "list", "data": AVAILABLE_MODELS}
        entry = resolve_model_metadata(clean_id)
        if entry is None:
            return _openai_error_response(
                status_code=404,
                message=f"The model '{clean_id}' does not exist",
                error_type="invalid_request_error",
                code="model_not_found",
            )
        return entry

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

    @app.post("/v1/files", dependencies=[Depends(verify_api_key)])
    @app.post("/files", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def upload_file(raw_request: Request) -> Any:
        ctype = raw_request.headers.get("content-type", "")
        body = await raw_request.body()
        try:
            if "multipart/form-data" in ctype.lower():
                fields, uploaded = parse_multipart_form(ctype, body)
                if not uploaded:
                    raise InvalidAttachmentError("未在表单中找到上传文件字段（file）。")
                item = uploaded[0]
                meta = file_store.save(
                    item["data"],
                    filename=fields.get("filename") or item.get("filename"),
                    declared_mime=item.get("content_type"),
                    purpose=fields.get("purpose") or "user_data",
                )
            else:
                payload = json.loads(body or b"{}")
                raw_data = payload.get("file_data") or payload.get("data") or payload.get("content") or ""
                declared_mime, raw_bytes = decode_base64_payload(raw_data)
                meta = file_store.save(
                    raw_bytes,
                    filename=payload.get("filename"),
                    declared_mime=payload.get("mime_type") or declared_mime,
                    purpose=payload.get("purpose") or "user_data",
                )
            return {k: v for k, v in meta.items() if k != "path"}
        except (InvalidAttachmentError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "error_code", "invalid_file_upload")
            return _openai_error_response(400, str(exc), "invalid_request_error", code)

    @app.get("/v1/files", dependencies=[Depends(verify_api_key)])
    @app.get("/files", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def list_uploaded_files(purpose: Optional[str] = None) -> Dict[str, Any]:
        return {"object": "list", "data": file_store.list_files(purpose=purpose)}

    @app.get("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
    @app.get("/files/{file_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def retrieve_uploaded_file(file_id: str) -> Any:
        try:
            meta = file_store.get_meta(file_id)
            return {k: v for k, v in meta.items() if k != "path"}
        except FileNotFoundError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "file_not_found")

    @app.get("/v1/files/{file_id}/content", dependencies=[Depends(verify_api_key)])
    @app.get("/files/{file_id}/content", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def download_uploaded_file(file_id: str) -> Any:
        try:
            meta = file_store.get_meta(file_id)
            return FileResponse(meta["path"], media_type=meta["mime_type"], filename=meta["filename"])
        except FileNotFoundError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "file_not_found")

    @app.delete("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
    @app.delete("/files/{file_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def delete_uploaded_file(file_id: str) -> Any:
        if not file_store.delete(file_id):
            return _openai_error_response(404, f"文件不存在：{file_id}", "invalid_request_error", "file_not_found")
        return {"id": file_id, "object": "file", "deleted": True}

    @app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
    async def image_generations(payload: Dict[str, Any]) -> Any:
        try:
            images = payload.get("images") or []
            if not isinstance(images, list):
                raise ValueError("images 必须是图片 data URL 或 file_id 数组。")
            attachments = collect_attachments(
                [ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": image}} for image in images])],
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
            )
            return await generate_images(payload, hub, context_builder, cfg, image_output_dir, attachments, file_store=file_store)
        except (InvalidAttachmentError, ValueError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_image_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "image_generation_failed")

    @app.post("/v1/images/edits", dependencies=[Depends(verify_api_key)])
    async def image_edits(raw_request: Request) -> Any:
        ctype = raw_request.headers.get("content-type", "")
        body = await raw_request.body()
        try:
            if "multipart/form-data" in ctype.lower():
                fields, uploaded = parse_multipart_form(ctype, body)
                if not uploaded:
                    raise ValueError("请上传需要编辑的图片（image 字段）。")
                parts = []
                for item in uploaded:
                    if item["field"] == "mask":
                        continue
                    saved = file_store.save(
                        item["data"],
                        filename=item["filename"],
                        declared_mime=item["content_type"],
                        purpose="vision",
                        expected_category="image",
                    )
                    parts.append({"type": "image_url", "image_url": {"url": saved["id"]}})
                payload = {
                    "prompt": fields.get("prompt", ""),
                    "model": fields.get("model") or cfg.default_model,
                    "n": int(fields.get("n", "1") or "1"),
                    "size": fields.get("size", "auto"),
                    "response_format": fields.get("response_format", "b64_json"),
                }
                attachments = collect_attachments(
                    [ChatMessage(role="user", content=parts)],
                    file_store=file_store,
                    callback_base_url=callback_origin,
                    image_output_dir=image_output_dir,
                )
            else:
                payload = json.loads(body or b"{}")
                images = payload.get("images") or ([payload["image"]] if payload.get("image") else [])
                if not isinstance(images, list) or not images:
                    raise ValueError("请提供需要编辑的图片（image 或 images）。")
                attachments = collect_attachments(
                    [ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": img}} for img in images])],
                    file_store=file_store,
                    callback_base_url=callback_origin,
                    image_output_dir=image_output_dir,
                )
            return await generate_images(payload, hub, context_builder, cfg, image_output_dir, attachments, file_store=file_store)
        except (InvalidAttachmentError, ValueError, json.JSONDecodeError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_image_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "image_generation_failed")

    @app.get("/v1/images/files/{filename}", dependencies=[Depends(verify_api_key)])
    async def image_file(filename: str) -> Any:
        if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|webp|gif|bmp|svg|avif|ico)", filename) or not (image_output_dir / filename).is_file():
            return _openai_error_response(404, "图片文件不存在。", "invalid_request_error", "image_not_found")
        return FileResponse(image_output_dir / filename)

    @app.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)])
    async def video_generations(payload: Dict[str, Any]) -> Any:
        try:
            raw_refs = payload.get("images") or payload.get("files") or []
            if not isinstance(raw_refs, list):
                raise ValueError("images/files 必须是数组。")
            attachments = collect_attachments(
                [ChatMessage(role="user", content=[{"type": "input_file", "file_data": ref} for ref in raw_refs])],
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
            ) if raw_refs else []
            return await generate_videos(payload, hub, context_builder, cfg, image_output_dir, file_store, attachments)
        except (InvalidAttachmentError, ValueError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_video_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "video_generation_failed")

    async def _handle_audio_task(raw_request: Request, translate_to_english: bool = False) -> Any:
        ctype = raw_request.headers.get("content-type", "")
        body = await raw_request.body()
        try:
            if "multipart/form-data" in ctype.lower():
                fields, uploaded = parse_multipart_form(ctype, body)
                if not uploaded:
                    raise InvalidAttachmentError("请上传音频文件（file 字段）。", error_code="invalid_audio_input")
                saved = file_store.save(
                    uploaded[0]["data"],
                    filename=uploaded[0]["filename"],
                    declared_mime=uploaded[0]["content_type"],
                    purpose="audio",
                    expected_category="audio",
                )
                fid = saved["id"]
                model_req = fields.get("model") or cfg.default_model
                hint = fields.get("prompt", "").strip()
                lang = fields.get("language", "").strip()
                resp_fmt = (fields.get("response_format") or "json").strip().lower()
            else:
                payload = json.loads(body or b"{}")
                fid = payload.get("file_id")
                if not fid:
                    raw_audio = payload.get("audio") or payload.get("data") or payload.get("file_data") or ""
                    decl_mime, raw_bytes = decode_base64_payload(raw_audio, error_code="invalid_audio_input")
                    saved = file_store.save(
                        raw_bytes,
                        filename=payload.get("filename"),
                        declared_mime=payload.get("mime_type") or decl_mime,
                        purpose="audio",
                        expected_category="audio",
                    )
                    fid = saved["id"]
                model_req = payload.get("model") or cfg.default_model
                hint = str(payload.get("prompt") or "").strip()
                lang = str(payload.get("language") or "").strip()
                resp_fmt = str(payload.get("response_format") or "json").strip().lower()

            if resp_fmt not in {"json", "text", "verbose_json", "srt", "vtt"}:
                raise ValueError("response_format 仅支持 json、text、verbose_json、srt、vtt。")
            if not hub.has_ready_client():
                raise BridgeUnavailableError("没有连接可用的 Gemini Enterprise 页面。")

            if translate_to_english:
                instruction = "Listen carefully to the attached audio and translate the spoken content into accurate English. Output ONLY the translated transcript text without commentary or markdown fences."
            elif resp_fmt in {"srt", "vtt"}:
                instruction = f"Transcribe the attached audio accurately in {resp_fmt.upper()} subtitle format. Output ONLY valid {resp_fmt.upper()} content without markdown fences or commentary."
            else:
                lang_clause = f" Language hint: {lang}." if lang else ""
                instruction = f"Transcribe the attached audio verbatim.{lang_clause} Output ONLY the exact spoken transcript without commentary, prefixes, or markdown fences."
            if hint:
                instruction += f" Context/spelling hint: {hint}"

            msg = ChatMessage(role="user", content=[
                {"type": "input_text", "text": instruction},
                {"type": "input_audio", "file_id": fid},
            ])
            attachments = collect_attachments([msg], file_store=file_store, callback_base_url=callback_origin, image_output_dir=image_output_dir)
            async with hub.acquire_slot(explicit_session_id="audio_" + uuid.uuid4().hex, queue_timeout=cfg.queue_timeout_seconds) as slot:
                context_builder.clear_session(slot.slot_key)
                try:
                    raw_reply = await hub.dispatch_complete_on_slot(
                        slot=slot,
                        prompt=msg.text_content(),
                        target_model=resolve_target_web_model(model_req),
                        reset_chat=True,
                        suppress_citations=True,
                        request_timeout=cfg.request_timeout_seconds,
                        attachments=attachments,
                    )
                finally:
                    context_builder.clear_session(slot.slot_key)
            cleaned_text = parse_assistant_output(raw_reply).content.strip()
            if resp_fmt in {"text", "srt", "vtt"}:
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(cleaned_text)
            if resp_fmt == "verbose_json":
                return {"task": "translate" if translate_to_english else "transcribe", "language": lang or "auto", "text": cleaned_text}
            return {"text": cleaned_text}
        except (InvalidAttachmentError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "error_code", "invalid_audio_input")
            return _openai_error_response(400, str(exc), "invalid_request_error", code)
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "audio_processing_failed")

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(verify_api_key)])
    async def audio_transcriptions(raw_request: Request) -> Any:
        return await _handle_audio_task(raw_request, translate_to_english=False)

    @app.post("/v1/audio/translations", dependencies=[Depends(verify_api_key)])
    async def audio_translations(raw_request: Request) -> Any:
        return await _handle_audio_task(raw_request, translate_to_english=True)

    @app.post("/v1/responses", dependencies=[Depends(verify_api_key)])
    @app.post("/responses", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def responses(payload: Dict[str, Any], raw_request: Request) -> Any:
        try:
            request, mapping, history = prepare_request(payload, response_store)
        except (ResponsesError, ValidationError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_responses_request")
        try:
            upstream = await chat_completions(
                request, x_session_id=raw_request.headers.get("x-session-id"),
                x_bridge_session=raw_request.headers.get("x-bridge-session"))
            return await translate_response(upstream, payload, request.model or cfg.default_model, mapping, history, response_store,
                image_generator=lambda prompt: generate_images(
                    {"prompt": prompt}, hub, context_builder, cfg, image_output_dir,
                    [a for a in collect_attachments(request.messages, file_store=file_store, callback_base_url=callback_origin, image_output_dir=image_output_dir) if a["category"] == "image"],
                    file_store=file_store,
                ))
        except (ResponsesError, BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "response_execution_failed")

    @app.get("/v1/responses/{response_id}", dependencies=[Depends(verify_api_key)])
    async def retrieve_response(response_id: str) -> Any:
        try:
            return response_store.get(response_id)[0]
        except ResponsesError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "response_not_found")

    @app.delete("/v1/responses/{response_id}", dependencies=[Depends(verify_api_key)])
    async def delete_response(response_id: str) -> Any:
        if not response_store.delete(response_id):
            return _openai_error_response(404, "会话记录不存在。", "invalid_request_error", "response_not_found")
        return {"id": response_id, "object": "response.deleted", "deleted": True}

    @app.post("/v1/responses/input_tokens", dependencies=[Depends(verify_api_key)])
    async def response_input_tokens(payload: Dict[str, Any]) -> Any:
        try:
            request, _, _ = prepare_request(payload, response_store)
            text = prompt_for_usage(request)
            return {"object": "response.input_tokens", "input_tokens": estimate_tokens(text)}
        except (ResponsesError, ValidationError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_token_request")

    @app.post("/v1/responses/compact", dependencies=[Depends(verify_api_key)])
    async def compact_response(payload: Dict[str, Any]) -> Any:
        try:
            request, _, _ = prepare_request({**payload, "tools": [], "stream": False}, response_store)
            request.messages, recent_tool_rounds = split_checkpoint_history(request.messages)
            request.messages.append(ChatMessage(role="user", content=COMPACTION_INSTRUCTIONS))
            result = await chat_completions(request, x_session_id=None, x_bridge_session=None)
            if isinstance(result, JSONResponse):
                if result.status_code >= 400:
                    return result
                result = json.loads(result.body)
            summary = result["choices"][0]["message"].get("content") or ""
            if not summary.strip():
                return _openai_error_response(502, "压缩结果为空。", "upstream_error", "empty_compaction")
            token = "bridge_compact_" + uuid.uuid4().hex
            summaries = [m for m in request.messages if m.role in {"system", "developer"}]
            summaries.append(ChatMessage(role="user", content="[Previous session summary]\n" + summary))
            summaries.extend(recent_tool_rounds)
            response_store.put(
                token,
                {},
                compact_messages_for_store([m.model_dump(exclude_none=True) for m in summaries], file_store),
            )
            return {"id": "cmp_" + uuid.uuid4().hex, "object": "response.compaction", "created_at": int(time.time()),
                    "output": [{"id": "cmpitem_" + uuid.uuid4().hex, "type": "compaction", "encrypted_content": token}],
                    "usage": {"input_tokens": result.get("usage", {}).get("prompt_tokens", 0),
                              "output_tokens": result.get("usage", {}).get("completion_tokens", 0),
                              "total_tokens": result.get("usage", {}).get("total_tokens", 0)}}
        except (ResponsesError, ValidationError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_compaction_request")

    @app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
    @app.post("/chat/completions", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def chat_completions(
        request: ChatCompletionRequest,
        x_session_id: Optional[str] = Header(default=None),
        x_bridge_session: Optional[str] = Header(default=None),
    ) -> Any:
        req_started_mono = time.monotonic()
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
        include_usage = bool(request.stream_options and request.stream_options.include_usage)

        norm_tools = normalize_request_tools(request.tools, request.functions)
        norm_tool_choice = normalize_tool_choice(request.tool_choice, request.function_call)
        tools_active = is_tool_generation_enabled(norm_tools, norm_tool_choice)
        json_mode_active = isinstance(request.response_format, dict) and str(
            request.response_format.get("type") or ""
        ).lower() in {"json_object", "json_schema"}
        suppress_citations = tools_active or json_mode_active

        usage_prompt = prompt_for_usage(request)
        extra_attachment_tokens = sum(
            estimate_attachment_tokens(att)
            for m in request.messages
            for att in m.extract_attachments()
        )

        # ---------------- Option 1: Official GCloud Upstream ----------------
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
                    prompt_text=usage_prompt,
                    created=created_ts,
                    tool_calls=parsed.tool_calls or None,
                    finish_reason=parsed.finish_reason,
                )
                if extra_attachment_tokens > 0:
                    resp_obj["usage"]["prompt_tokens"] += extra_attachment_tokens
                    resp_obj["usage"]["total_tokens"] = (
                        resp_obj["usage"]["prompt_tokens"] + resp_obj["usage"]["completion_tokens"]
                    )
                usage_tracker.record_request(
                    requested_model=model_name,
                    prompt_tokens=resp_obj["usage"]["prompt_tokens"],
                    completion_tokens=resp_obj["usage"]["completion_tokens"],
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
                    gcloud_u = build_usage(usage_prompt, completion_usage_str)
                    if extra_attachment_tokens > 0:
                        gcloud_u["prompt_tokens"] += extra_attachment_tokens
                        gcloud_u["total_tokens"] = gcloud_u["prompt_tokens"] + gcloud_u["completion_tokens"]
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
                except (GCloudUpstreamError, ToolProtocolError) as exc:
                    yield "data: " + json.dumps({"error": {"message": str(exc), "type": "upstream_error", "code": "gcloud_failed"}}, ensure_ascii=False) + "\n\n"
                    return

                stop_chunk = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=model_name,
                    delta={},
                    finish_reason=finish_reason,
                    created=created_ts,
                )
                yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                if include_usage:
                    u_obj = build_usage(usage_prompt, completion_usage_str)
                    if extra_attachment_tokens > 0:
                        u_obj["prompt_tokens"] += extra_attachment_tokens
                        u_obj["total_tokens"] = u_obj["prompt_tokens"] + u_obj["completion_tokens"]
                    usage_chunk = build_chat_completion_chunk(
                        request_id=completion_id,
                        model=model_name,
                        delta={},
                        created=created_ts,
                        usage=u_obj,
                    )
                    yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                with_keepalive(gcloud_sse()),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Validate browser-bound multimodal attachments upfront before checking browser readiness
        try:
            collect_attachments(
                request.messages,
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
            )
        except InvalidAttachmentError as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", exc.error_code)
        except ValueError as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_attachment")

        def _select_turn_attachments(prepared: Any) -> Any:
            active_msgs = [
                request.messages[idx]
                for idx in getattr(prepared, "active_message_indices", [])
                if 0 <= idx < len(request.messages)
            ]
            collected = collect_attachments(
                active_msgs,
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
            )
            return collected if collected else list(prepared.attachments or [])

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
                async with conversation_slot(
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
                    record_context(request, prepared)
                    raw_out_images: list[dict[str, Any]] = []
                    raw_out_files: list[dict[str, Any]] = []
                    raw_reply = await hub.dispatch_complete_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                        attachments=_select_turn_attachments(prepared),
                        output_images=raw_out_images,
                        output_files=raw_out_files,
                    )
                    saved_outputs = persist_browser_outputs(
                        raw_out_images, raw_out_files, image_output_dir, file_store, callback_origin
                    )
                    try:
                        parsed = parse_assistant_output(
                            raw_reply,
                            tools=norm_tools,
                            tool_choice=norm_tool_choice,
                            parallel_tool_calls=request.parallel_tool_calls,
                            response_format=request.response_format,
                        )
                    except ToolProtocolError:
                        logger.warning("Repairing invalid browser tool reply on %s", session_key)
                        raw_reply = await hub.dispatch_complete_on_slot(
                            slot=slot,
                            prompt=TOOL_OUTPUT_REPAIR_PROMPT,
                            target_model=target_web_model,
                            reset_chat=False,
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
                    if saved_outputs and not parsed.has_tool_calls and not json_mode_active:
                        md_links = format_artifacts_markdown(saved_outputs, parsed.content or "")
                        if md_links and (not tools_active or not (parsed.content or "").strip()):
                            parsed.content = ((parsed.content or "").rstrip() + "\n\n" + md_links).strip()
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
                            estimate_tokens(usage_prompt),
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
            except (BridgeExecutionError, ToolProtocolError) as exc:
                return _openai_error_response(502, str(exc), "bridge_execution_error", "dom_execution_failed")

            resp_payload = build_chat_completion_response(
                request_id=completion_id,
                model=raw_model_name,
                reply_text=parsed.content,
                prompt_text=usage_prompt,
                created=created_ts,
                tool_calls=parsed.tool_calls or None,
                finish_reason=parsed.finish_reason,
                attachments=saved_outputs or None,
            )
            if extra_attachment_tokens > 0:
                resp_payload["usage"]["prompt_tokens"] += extra_attachment_tokens
                resp_payload["usage"]["total_tokens"] = (
                    resp_payload["usage"]["prompt_tokens"] + resp_payload["usage"]["completion_tokens"]
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
            prompt_for_usage_str = usage_prompt
            last_sse_ts = time.monotonic()
            emitted_content: list[str] = []

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

                async with conversation_slot(
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
                    record_context(request, prepared)
                    stream_out_images: list[dict[str, Any]] = []
                    stream_out_files: list[dict[str, Any]] = []
                    async for delta in hub.dispatch_stream_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                        attachments=_select_turn_attachments(prepared),
                        output_images=stream_out_images,
                        output_files=stream_out_files,
                    ):
                        if not delta:
                            if time.monotonic() - last_sse_ts >= 14.5:
                                yield ": keepalive\n\n"
                                last_sse_ts = time.monotonic()
                            continue

                        for safe_piece in processor.feed(delta):
                            emitted_content.append(safe_piece)
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=raw_model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            last_sse_ts = time.monotonic()

                    try:
                        tail_pieces, parsed = processor.finalize()
                    except ToolProtocolError:
                        logger.warning("Repairing invalid browser tool stream on %s", session_key)
                        processor = ToolStreamProcessor(
                            tools=norm_tools,
                            tool_choice=norm_tool_choice,
                            parallel_tool_calls=request.parallel_tool_calls,
                            response_format=request.response_format,
                        )
                        async for delta in hub.dispatch_stream_on_slot(
                            slot=slot,
                            prompt=TOOL_OUTPUT_REPAIR_PROMPT,
                            target_model=target_web_model,
                            reset_chat=False,
                            suppress_citations=suppress_citations,
                            request_timeout=cfg.request_timeout_seconds,
                        ):
                            if delta:
                                processor.feed(delta)
                            if time.monotonic() - last_sse_ts >= 14.5:
                                yield ": keepalive\n\n"
                                last_sse_ts = time.monotonic()
                        tail_pieces, parsed = processor.finalize()
                        tail_pieces = [parsed.content] if parsed.content else []
                        parsed.content = ("".join(emitted_content) + (parsed.content or "")).strip() or None

                    for raw_piece in tail_pieces:
                        for safe_piece in split_text_slices(raw_piece):
                            chunk = build_chat_completion_chunk(
                                request_id=completion_id,
                                model=raw_model_name,
                                delta={"content": safe_piece},
                                created=created_ts,
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    saved_stream_outputs = persist_browser_outputs(
                        stream_out_images, stream_out_files, image_output_dir, file_store, callback_origin
                    )
                    if saved_stream_outputs:
                        if not parsed.has_tool_calls and not json_mode_active:
                            md_links = format_artifacts_markdown(saved_stream_outputs, parsed.content or "")
                            if md_links and (not tools_active or not (parsed.content or "").strip()):
                                extra = ("\n\n" if (parsed.content or "").strip() else "") + md_links
                                parsed.content = (parsed.content or "") + extra
                                chunk = build_chat_completion_chunk(
                                    request_id=completion_id,
                                    model=raw_model_name,
                                    delta={"content": extra, "attachments": saved_stream_outputs},
                                    created=created_ts,
                                )
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            else:
                                chunk = build_chat_completion_chunk(
                                    request_id=completion_id,
                                    model=raw_model_name,
                                    delta={"attachments": saved_stream_outputs},
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
            except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
                yield "data: " + json.dumps({"error": {"message": str(exc), "type": "bridge_error", "code": "browser_failed"}}, ensure_ascii=False) + "\n\n"
                return

            stop_chunk = build_chat_completion_chunk(
                request_id=completion_id,
                model=raw_model_name,
                delta={},
                finish_reason=finish_reason,
                created=created_ts,
            )
            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"

            if include_usage:
                u_obj = build_usage(prompt_for_usage_str, completion_usage_str)
                if extra_attachment_tokens > 0:
                    u_obj["prompt_tokens"] += extra_attachment_tokens
                    u_obj["total_tokens"] = u_obj["prompt_tokens"] + u_obj["completion_tokens"]
                usage_chunk = build_chat_completion_chunk(
                    request_id=completion_id,
                    model=raw_model_name,
                    delta={},
                    created=created_ts,
                    usage=u_obj,
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
            with_keepalive(browser_sse()),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    runtime_cfg = BridgeSettings.from_env()
    if os.getenv("BRIDGE_SYNC_COPILOT_LIMITS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        try:
            from sync_client_limits import sync_copilot_provider_limits

            sync_res = sync_copilot_provider_limits(bridge_port=runtime_cfg.port)
            updated_rows = sync_res.get("models_updated") or []
            if updated_rows:
                logger.info(
                    "Synced 1M prompt / 65K output token limits for %d Copilot BYOK model(s): %s",
                    len(updated_rows),
                    ", ".join(m["model_id"] for m in updated_rows),
                )
        except Exception as exc:  # pragma: no cover
            logger.debug("Optional Copilot BYOK token limit sync skipped: %s", exc)
    uvicorn.run(
        app,
        host=runtime_cfg.host,
        port=runtime_cfg.port,
        ws_max_size=16 * 1024 * 1024,
        h11_max_incomplete_event_size=16 * 1024 * 1024,
    )

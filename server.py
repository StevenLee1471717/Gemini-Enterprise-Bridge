"""FastAPI OpenAI-compatible Gateway for Gemini Enterprise (Vertex AI Search / Agentspace)."""

from __future__ import annotations

import json
import logging
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
    parse_model_and_session,
    resolve_target_web_model,
)
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


def create_app(settings: Optional[BridgeSettings] = None) -> FastAPI:
    cfg = settings or BridgeSettings.from_env()
    cb_host = "127.0.0.1" if cfg.host in {"0.0.0.0", "::"} else cfg.host
    hub = BrowserBridgeHub(
        include_citations=cfg.include_citations,
        callback_base_url=f"http://{cb_host}:{cfg.port}",
        max_parallel_tabs=cfg.max_parallel_tabs,
        auto_spawn_tabs=cfg.auto_spawn_tabs,
        session_affinity_ttl_seconds=cfg.session_affinity_ttl_seconds,
    )
    context_builder = ConversationContextBuilder(
        default_mode=cfg.context_mode,
        auto_reset_new_chat=cfg.auto_reset_new_chat,
    )
    gcloud = GCloudVertexProvider(cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if cfg.auto_export_extension:
            try:
                ext_path = export_extension_bundle(Path(__file__).resolve().parent)
                logger.info("Chrome/Edge MV3 扩展目录已就绪: %s", ext_path)
            except Exception as exc:  # pragma: no cover
                logger.warning("自动导出 extension 目录失败（不影响主目录加载）: %s", exc)
        logger.info(
            "Gemini Enterprise Bridge 已启动: http://%s:%d/ (OpenAI Base URL: http://%s:%d/v1)",
            cfg.host,
            cfg.port,
            cfg.host,
            cfg.port,
        )
        yield

    app = FastAPI(
        title="Gemini Enterprise Web-to-API Bridge",
        version="1.0.0",
        description="Browser-in-the-loop OpenAI-compatible proxy for Google Cloud Vertex AI Search / Agentspace",
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.hub = hub
    app.state.context_builder = context_builder
    app.state.gcloud = gcloud

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
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
            "browser_connected": hub.has_ready_client(),
            "gcloud_configured": gcloud.is_configured(),
            "backend_mode": cfg.backend_mode,
        }

    @app.get("/api/status")
    async def api_status() -> Dict[str, Any]:
        return {
            "status": "ok",
            "backend_mode": cfg.backend_mode,
            "context_mode": cfg.context_mode,
            "gcloud_configured": gcloud.is_configured(),
            "hub": hub.status_snapshot(),
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
                        await websocket.send_json(reply_packet)
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
        return {"ok": True, "command": command}

    @app.post("/api/bridge/event")
    async def receive_http_bridge_event(payload: BridgeEventPayload) -> Dict[str, Any]:
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        raw["type"] = payload.event
        ack = await hub.handle_browser_event(payload.client_id, raw)
        return {"ok": True, "ack": ack}

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
                return build_chat_completion_response(
                    request_id=completion_id,
                    model=model_name,
                    reply_text=parsed.content,
                    prompt_text=prompt_for_usage,
                    created=created_ts,
                    tool_calls=parsed.tool_calls or None,
                    finish_reason=parsed.finish_reason,
                )

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
                    context_builder.record_completed_turn(
                        session_key=session_key,
                        sent_history=prepared.normalized_history,
                        assistant_reply=parsed.content or "",
                        assistant_tool_calls=parsed.tool_calls or None,
                        active_model=target_web_model,
                        tools_fingerprint=prepared.tools_fingerprint,
                        system_fingerprint=prepared.system_fingerprint,
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
                            active_model=target_web_model,
                            tools_fingerprint=prepared.tools_fingerprint,
                            system_fingerprint=prepared.system_fingerprint,
                        )
                    else:
                        full_reply = (parsed.content or "").strip()
                        completion_usage_str = full_reply
                        if full_reply:
                            context_builder.record_completed_turn(
                                session_key=session_key,
                                sent_history=prepared.normalized_history,
                                assistant_reply=full_reply,
                                active_model=target_web_model,
                                tools_fingerprint=prepared.tools_fingerprint,
                                system_fingerprint=prepared.system_fingerprint,
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

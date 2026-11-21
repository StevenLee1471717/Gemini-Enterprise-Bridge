"""FastAPI OpenAI-compatible Gateway for Gemini Enterprise (Vertex AI Search / Agentspace)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Sequence

from pydantic import ValidationError

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, FileResponse

from bridge_hub import (
    BridgeExecutionError,
    BridgeTimeoutError,
    BridgeUnavailableError,
    BrowserBridgeHub,
)
from config import BridgeSettings
from agent_policy import (CARBON_CONTEXT_WINDOW, CARBON_AUTO_COMPACT_TOKENS,
                          COMPACTION_INSTRUCTIONS, CODING_WORKFLOW, has_coding_tools,
                          has_tool_history,
                          reading_diagnostics, split_checkpoint_history)
from context_builder import ConversationContextBuilder, PendingCompactionCleanup, extract_dialogue_title
from dashboard_html import DASHBOARD_HTML, get_dashboard_html
from distribution import (
    DistributionAuthError,
    DistributionManager,
    DistributionQuotaError,
    HOST_TENANT_ID,
    RequestPrincipal,
    extract_client_ip,
    extract_bearer_or_api_key,
    is_direct_local_connection,
    resolve_public_base_url,
)
from tunnel_manager import PublicTunnelManager
from public_status import PublicStatusMonitor, get_public_status_html
from extension_exporter import export_extension_bundle
from gcloud_provider import GCloudUpstreamError, GCloudVertexProvider
from logging_utils import LOG_MANAGER, request_id_var, summarize_for_debug
from platform_compat import reconfigure_stdio_utf8
from schemas import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL_ID,
    estimate_tokens,
    BridgeCallbackPayload,
    BridgeEventPayload,
    BridgeRegisterRequest,
    ChatCompletionRequest,
    ChatMessage,
    build_chat_completion_chunk,
    build_chat_completion_response,
    build_usage,
    normalize_default_model,
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
from version import APP_VERSION
from media_inputs import (
    FileStore,
    InvalidAttachmentError,
    collect_attachments,
    collect_rebuild_attachments,
    has_any_attachment_parts,
    historical_image_markers,
    reference_historical_images,
    compact_messages_for_store,
    decode_base64_payload,
    parse_multipart_form,
    resolve_mime_and_name,
)
from image_outputs import generate_images
from media_outputs import format_artifacts_markdown, generate_videos, persist_browser_outputs
from responses_api import (
    ResponseStore,
    ResponsesError,
    prepare_request,
    translate_response,
)
from streaming_utils import ClosingStreamingResponse, with_keepalive
from usage_ledger import UsageLedger, estimate_attachment_input_tokens
from tool_calling import (
    ToolStreamProcessor,
    ToolProtocolError,
    BrowserTaskFailure,
    build_sse_tool_call_deltas,
    build_tool_system_instructions,
    build_response_format_instructions,
    is_tool_generation_enabled,
    normalize_request_tools,
    normalize_tool_choice,
    parse_assistant_output,
    split_text_slices,
)

reconfigure_stdio_utf8()
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
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
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
    LOG_MANAGER.configure(
        level=cfg.log_level,
        debug=cfg.debug,
        log_file=cfg.log_file,
        log_payloads=cfg.log_payloads,
    )
    root_dir = Path(__file__).resolve().parent
    updater = update_manager or UpdateManager(
        root_dir=root_dir,
        current_version=APP_VERSION,
        repo=cfg.github_repo,
    )
    cb_host = "127.0.0.1" if cfg.host in {"0.0.0.0", "::"} else cfg.host
    cb_host_url = f"[{cb_host}]" if (":" in cb_host and not cb_host.startswith("[")) else cb_host
    hub = BrowserBridgeHub(
        include_citations=cfg.include_citations,
        callback_base_url=f"http://{cb_host_url}:{cfg.port}",
        max_parallel_tabs=cfg.max_parallel_tabs,
        auto_spawn_tabs=cfg.auto_spawn_tabs,
        session_affinity_ttl_seconds=cfg.session_affinity_ttl_seconds,
        backend_version=updater.current_version,
    )
    context_builder = ConversationContextBuilder(
        default_mode=cfg.context_mode,
        auto_reset_new_chat=cfg.auto_reset_new_chat,
        archive_path=cfg.session_archive_path,
    )
    gcloud = GCloudVertexProvider(cfg)
    price_overrides: Dict[str, Any] = {}
    if cfg.price_overrides_json:
        try:
            loaded_ov = json.loads(cfg.price_overrides_json)
            if isinstance(loaded_ov, dict):
                price_overrides = loaded_ov
        except Exception as exc:
            logger.warning("解析 BRIDGE_PRICE_OVERRIDES_JSON 失败，使用默认参考单价: %s", exc)
    ledger_file = (
        cfg.usage_stats_path
        or cfg.usage_ledger_path
        or (str(Path(cfg.generated_dir) / "usage_ledger.json") if cfg.generated_dir else None)
    )
    usage_ledger = UsageLedger(
        storage_path=ledger_file,
        usd_to_cny_rate=cfg.usd_to_cny_rate,
        price_overrides=price_overrides,
    )
    dist_file = cfg.distribution_path or (
        str(Path(cfg.generated_dir) / "client_distribution.json")
        if cfg.generated_dir
        else str(Path(__file__).resolve().parent / "generated" / "client_distribution.json")
    )
    distribution = DistributionManager(
        storage_path=dist_file,
        default_host_reserved_slots=cfg.host_reserved_slots,
    )
    tunnel_file = cfg.tunnel_state_path or str(Path(dist_file).parent / "tunnel_state.json")
    tunnel_manager = PublicTunnelManager(
        target_port=cfg.port,
        state_path=tunnel_file,
        on_url_changed=lambda url: distribution.update_policy(public_base_url=url),
    )
    status_history_file = str(Path(dist_file).parent / "public_status_history.json")
    public_status_monitor = PublicStatusMonitor(storage_path=status_history_file)
    init_tun = tunnel_manager.snapshot()
    if init_tun.get("running") and init_tun.get("active_url") and tunnel_manager.auto_sync_public_url and not distribution.public_base_url:
        distribution.update_policy(public_base_url=str(init_tun["active_url"]))
    _principal_var: contextvars.ContextVar[Optional[RequestPrincipal]] = contextvars.ContextVar(
        "bridge_principal", default=None
    )
    _public_base_url_var: contextvars.ContextVar[str] = contextvars.ContextVar(
        "bridge_public_base_url", default=""
    )
    _permit_held_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
        "bridge_permit_held", default=False
    )
    hub.on_slot_removed = context_builder.drop_slot
    hub.on_slot_migrated = context_builder.migrate_slot_key
    hub.on_slot_evicted = lambda s: context_builder.evict_slot_to_archive(
        s.slot_key,
        fallback_url=s.tab_url or "",
        fallback_explicit_session_id=s.pinned_session_id,
    )
    def _sync_extension_for_reload(_old_ext_ver: str) -> None:
        if cfg.auto_export_extension:
            try:
                export_extension_bundle(updater.root_dir)
            except Exception:
                pass

    hub.has_slot_conversation = context_builder.has_active_conversation
    hub.is_slot_ephemeral_probe = context_builder.is_ephemeral_probe_slot
    hub.is_slot_session_active = lambda s: context_builder.is_slot_session_active(s.slot_key, s.to_public_dict())
    hub.on_extension_version_reported = updater.record_extension_version
    hub.on_outdated_extension_detected = _sync_extension_for_reload
    _ledger_task_var: contextvars.ContextVar[str] = contextvars.ContextVar("bridge_ledger_task", default="chat")
    _consumed_compactions_var: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
        "bridge_consumed_compactions", default=()
    )
    _latest_compaction_rec_var: contextvars.ContextVar[Optional[PendingCompactionCleanup]] = contextvars.ContextVar(
        "bridge_latest_compaction_rec", default=None
    )
    _inflight_compaction_resets: set[tuple[str, int]] = set()
    _bg_tasks: set[asyncio.Task[Any]] = set()
    context_diagnostics: Dict[str, Any] = {}
    diagnostic_request: Optional[ChatCompletionRequest] = None

    def _extract_input_compaction_tokens(raw_input: Any) -> tuple[str, ...]:
        if not isinstance(raw_input, list):
            return ()
        tokens: list[str] = []
        for item in raw_input:
            if isinstance(item, dict) and item.get("type") == "compaction":
                enc = item.get("encrypted_content")
                if isinstance(enc, str) and enc.strip():
                    tokens.append(enc.strip())
        return tuple(tokens)

    async def _auto_reset_compaction_slot(rec: PendingCompactionCleanup) -> None:
        key = (rec.slot_key, rec.compaction_seq)
        try:
            slot_obj = hub._slots.get(rec.slot_key)
            if slot_obj is None:
                claimed_missing = context_builder.claim_compaction_for_reset(rec.slot_key, rec.compaction_seq)
                if claimed_missing is not None:
                    context_builder.delete_archived_by_vertex_session_id(claimed_missing.vertex_session_id)
                return
            if slot_obj.is_busy or slot_obj.waiting_count > 0 or slot_obj.lock.locked():
                return
            claimed = context_builder.claim_compaction_for_reset(rec.slot_key, rec.compaction_seq)
            if claimed is None:
                return
            context_builder.delete_archived_by_vertex_session_id(claimed.vertex_session_id)
            label = f"#{slot_obj.tab_id}" if slot_obj.tab_id is not None else claimed.slot_key
            try:
                await hub.reset_worker_slot(
                    claimed.slot_key,
                    reason=f"新会话已接管压缩记录，自动重置临时压缩席位 {label}",
                    on_reset_locked=lambda: context_builder.invalidate_session(claimed.slot_key),
                )
            except KeyError:
                context_builder.invalidate_session(claimed.slot_key)
        except RuntimeError as exc:
            logger.debug("Skipped auto-reset on busy compaction slot %s: %s", rec.slot_key, exc)
        except Exception as exc:
            logger.warning("Auto-reset on compaction slot %s failed: %s", rec.slot_key, exc)
        finally:
            _inflight_compaction_resets.discard(key)

    def schedule_compaction_resets(
        records: Sequence[PendingCompactionCleanup],
        *,
        exclude_slot_key: Optional[str] = None,
    ) -> None:
        if not records:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for rec in records:
            if exclude_slot_key and rec.slot_key == exclude_slot_key:
                continue
            key = (rec.slot_key, rec.compaction_seq)
            if key in _inflight_compaction_resets:
                continue
            _inflight_compaction_resets.add(key)
            task = loop.create_task(_auto_reset_compaction_slot(rec))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)

    def _active_model_hint(hub_snap: Dict[str, Any], ctx_mon: Dict[str, Any]) -> str:
        prim_m = str((ctx_mon.get("primary") or {}).get("active_model") or "")
        if prim_m:
            return prim_m
        ac = hub_snap.get("active_client")
        if isinstance(ac, dict) and ac.get("active_model"):
            return str(ac["active_model"])
        for ws_item in hub_snap.get("worker_slots") or []:
            if isinstance(ws_item, dict) and ws_item.get("active_model"):
                return str(ws_item["active_model"])
        return ""

    def sync_and_hydrate_idle_slots() -> bool:
        mutated = False
        for s in list(hub._slots.values()):
            if s.is_busy or s.waiting_count > 0 or s.lock.locked():
                continue
            if context_builder.reconcile_idle_slot_url(
                s.slot_key,
                s.tab_url or "",
                fallback_explicit_session_id=s.pinned_session_id,
            ):
                hub.clear_slot_affinity_only(s)
                mutated = True
        hydrated = context_builder.hydrate_live_slots_from_archive(hub.hydratable_slot_url_map())
        for h_slot_key, h_sid in hydrated.items():
            hub.restore_slot_affinity(
                h_slot_key,
                h_sid,
                last_used_at=context_builder.get_slot_last_updated_at(h_slot_key),
                owner_tenant_id=context_builder.get_slot_tenant_id(h_slot_key),
            )
            mutated = True
        return mutated

    def _effective_pool_capacity() -> int:
        ready_cnt = len(hub.list_ready_slot_keys())
        if ready_cnt > 0:
            return ready_cnt
        if gcloud.is_configured() and cfg.backend_mode in {"auto", "gcloud"}:
            return 8
        return 0

    def _public_gateway_health() -> tuple[bool, bool]:
        if cfg.backend_mode == "gcloud":
            return gcloud.is_configured(), False
        ready_keys = hub.list_ready_slot_keys()
        if ready_keys:
            busy_cnt = sum(
                1
                for k in ready_keys
                if (slot_obj := hub._slots.get(k)) is not None
                and (slot_obj.is_busy or slot_obj.waiting_count > 0)
            )
            return True, (busy_cnt >= len(ready_keys))
        if hub.has_ready_client():
            return True, False
        if cfg.backend_mode == "auto" and gcloud.is_configured():
            return True, False
        return False, False

    def build_live_ledger_snapshot(include_catalog: bool = False, hub_snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if sync_and_hydrate_idle_slots():
            hub_snap = hub.status_snapshot()
        snap = hub_snap if hub_snap is not None else hub.status_snapshot()
        ctx_mon = context_builder.get_context_snapshot(snap.get("worker_slots") or [])
        for slot_entry in ctx_mon.get("slots") or []:
            if isinstance(slot_entry, dict):
                slot_entry["tenant_name"] = distribution.get_tenant_label(slot_entry.get("tenant_id"))
        hint = _active_model_hint(snap, ctx_mon)
        pool_cap = len(hub.list_ready_slot_keys())
        return {
            "usage": usage_ledger.snapshot(
                active_model_hint=hint,
                default_model=cfg.default_model,
                include_catalog=include_catalog,
            ),
            "context_monitor": ctx_mon,
            "distribution": distribution.snapshot(include_secrets=True, pool_capacity=pool_cap),
            "tunnel": tunnel_manager.snapshot(),
            "sessions_overview": context_builder.list_sessions_overview(
                snap.get("worker_slots") or [],
                tenant_label_fn=distribution.get_tenant_label,
                limit=100,
            ),
        }

    def prompt_for_usage(request: ChatCompletionRequest) -> str:
        # Count the full logical context, even when only a delta is sent to the web.
        system_text, dialogue = context_builder._normalize_messages(request.messages)
        sections = [system_text, *(text for _, text in dialogue),
                    build_tool_system_instructions(request.tools or [], request.tool_choice, request.parallel_tool_calls),
                    build_response_format_instructions(request.response_format)]
        if (has_coding_tools(request.tools) and is_tool_generation_enabled(request.tools or [], request.tool_choice)
                and "[Coding workflow]" not in system_text):
            sections.append(CODING_WORKFLOW)
        return "\n\n".join(filter(None, sections))

    def record_context(request: ChatCompletionRequest, prepared, attachments=(), historical_images=(), sent_count: Optional[int] = None) -> None:
        if not current_principal().is_host:
            return
        nonlocal diagnostic_request
        diagnostic_request = request
        context_diagnostics.clear()
        uploaded_cnt = (
            sent_count
            if sent_count is not None
            else sum(att["id"] in prepared.prompt for att in attachments)
        )
        context_diagnostics.update(
            model=request.model or cfg.default_model, strategy=prepared.strategy_used,
            reset_chat=prepared.reset_chat, web_prompt_characters=len(prepared.prompt),
            estimated_logical_input_tokens=estimate_tokens(prompt_for_usage(request)),
            uploaded_attachments=uploaded_cnt,
            historical_images_referenced=len(historical_images),
            **reading_diagnostics(request.messages),
        )

    def record_reply_protocol(parsed, session_key, request):
        if diagnostic_request is request:
            context_diagnostics["reply_protocol"] = parsed.protocol
        if parsed.has_tool_calls:
            logger.info("Accepted client tool reply: protocol=%s slot=%s calls=%d argument_chars=%d",
                        parsed.protocol, session_key, len(parsed.tool_calls),
                        sum(len(call["function"]["arguments"]) for call in parsed.tool_calls))

    @asynccontextmanager
    async def conversation_slot(
        *,
        request_messages,
        model_name: str,
        explicit_session_id: Optional[str],
        tools_active: bool,
        queue_timeout: float,
        ephemeral_probe: bool = False,
        ephemeral_compaction: bool = False,
        tenant_id: str = HOST_TENANT_ID,
        strict_session_affinity: bool = False,
    ):
        sync_and_hydrate_idle_slots()

        ready_keys = hub.list_ready_slot_keys()
        matching_slots = (
            []
            if (ephemeral_probe or ephemeral_compaction)
            else context_builder.find_matching_slots(
                ready_keys,
                request_messages,
                model=model_name,
                tenant_id=tenant_id,
            )
        )
        if strict_session_affinity:
            matching_slots = [k for k in matching_slots if hub._slots[k].pinned_session_id == explicit_session_id]
        aff_slot = None if (ephemeral_probe or ephemeral_compaction) else hub.get_affinity_slot_key(explicit_session_id)
        archived_match = None
        if not (ephemeral_probe or ephemeral_compaction) and aff_slot not in ready_keys and not matching_slots:
            archived_match = context_builder.find_archived_session(
                request_messages,
                model=model_name,
                explicit_session_id=explicit_session_id,
                tenant_id=tenant_id,
            )
            if strict_session_affinity and archived_match and archived_match.explicit_session_id != explicit_session_id:
                archived_match = None

        ephemeral_aux = bool(
            ephemeral_compaction
            or (
                not strict_session_affinity
                and not tools_active
                and (
                    (aff_slot and context_builder.has_active_tool_session(aff_slot))
                    or any(context_builder.has_active_tool_session(k) for k in matching_slots)
                    or (
                        archived_match is not None
                        and bool(archived_match.tools_fp)
                        and len(archived_match.turns) >= 2
                    )
                )
            )
        )
        if ephemeral_aux:
            matching_slots = [
                k for k in matching_slots if not context_builder.has_active_tool_session(k)
            ]
            archived_match = None

        aff_slot_obj = hub._slots.get(aff_slot) if aff_slot else None
        has_compaction_resume = bool(
            _consumed_compactions_var.get()
            or context_builder.has_compaction_summary(request_messages)
        )
        avoid_affinity_slot = bool(
            not (strict_session_affinity or ephemeral_probe or ephemeral_compaction or ephemeral_aux)
            and tools_active
            and aff_slot_obj is not None
            and aff_slot in ready_keys
            and aff_slot not in matching_slots
            and context_builder.has_active_tool_session(aff_slot)
            and not context_builder.has_pending_recovery(aff_slot)
            and not has_compaction_resume
            and time.time() < aff_slot_obj.pending_tool_call_until
        )

        effective_sid = None if (ephemeral_aux or ephemeral_probe) else explicit_session_id
        async with hub.acquire_slot(
            explicit_session_id=explicit_session_id,
            matching_slot_keys=matching_slots,
            queue_timeout=queue_timeout,
            ephemeral_aux=ephemeral_aux,
            preferred_session_url=archived_match.session_url if archived_match is not None else None,
            ephemeral_probe=ephemeral_probe,
            tenant_id=tenant_id,
            avoid_affinity_slot=avoid_affinity_slot,
            strict_session_affinity=strict_session_affinity and not (ephemeral_probe or ephemeral_compaction),
        ) as slot:
            slot.last_dispatch_submitted = False
            slot.last_dispatch_tab_reset = False
            resumed_from_archive = False
            if ephemeral_probe:
                context_builder.mark_ephemeral_probe_slot(slot.slot_key)
            if archived_match is not None and not context_builder.has_active_conversation(slot.slot_key):
                resumed_ok = await hub.navigate_slot_to_session(slot, archived_match.session_url)
                if resumed_ok:
                    context_builder.restore_archived_to_slot(
                        slot.slot_key,
                        archived_match,
                        explicit_session_id=effective_sid,
                        session_url=slot.tab_url or archived_match.session_url,
                    )
                    resumed_from_archive = True
                else:
                    context_builder.delete_archived_session(archived_match.archive_key)
                    context_builder.invalidate_session(slot.slot_key)
            try:
                yield slot, effective_sid, resumed_from_archive
            except BaseException as exc:
                if isinstance(exc, ToolProtocolError):
                    err_kind = "protocol_error"
                elif isinstance(exc, BrowserTaskFailure):
                    err_kind = "upstream_task_failure"
                else:
                    err_kind = "interrupted"
                preserved = context_builder.record_failed_turn(
                    slot.slot_key,
                    kind=err_kind,
                    reason=str(exc),
                    submitted=bool(slot.last_dispatch_submitted),
                    tab_reset=bool(slot.last_dispatch_tab_reset),
                )
                if isinstance(exc, (ToolProtocolError, BrowserTaskFailure)):
                    logger.warning(
                        "Rejected browser reply on slot=%s (preserved=%s): %s",
                        slot.slot_key,
                        preserved,
                        exc,
                    )
                    hub.record_reply_error(slot, str(exc))
                slot.pending_tool_call_until = (
                    time.time() + 60.0
                    if preserved and context_builder.has_active_tool_session(slot.slot_key)
                    else 0.0
                )
                raise

    hub.usage_snapshot_provider = lambda: build_live_ledger_snapshot(include_catalog=False)

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
            "Gemini Enterprise Bridge v%s 已启动: http://%s:%d/ (OpenAI Base URL: http://%s:%d/v1, log_level=%s)",
            updater.current_version,
            cfg.host,
            cfg.port,
            cfg.host,
            cfg.port,
            LOG_MANAGER.current_level,
        )
        async def _status_heartbeat_loop() -> None:
            while True:
                try:
                    is_up, is_busy = _public_gateway_health()
                    ledger_stats = public_status_monitor._extract_sanitized_ledger_metrics(
                        usage_ledger, time.time()
                    )
                    public_status_monitor.record_sample(
                        online=is_up,
                        busy=is_busy,
                        latest_chat_ms=ledger_stats.get("gateway_chat_ms"),
                    )
                except Exception:
                    pass
                await asyncio.sleep(60.0)

        heartbeat_task = asyncio.create_task(_status_heartbeat_loop())
        try:
            yield
        finally:
            if bg_check_task is not None and not bg_check_task.done():
                bg_check_task.cancel()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            response_store.close()
            context_builder.close()

    app = FastAPI(
        title="Gemini Enterprise Web-to-API Bridge",
        version=updater.current_version,
        description="Browser-in-the-loop OpenAI-compatible proxy for Google Cloud Vertex AI Search / Agentspace",
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.hub = hub
    app.state.context_builder = context_builder
    app.state.gcloud = gcloud
    app.state.usage_ledger = usage_ledger
    app.state.update_manager = updater
    app.state.log_manager = LOG_MANAGER
    app.state.distribution = distribution
    app.state.tunnel = tunnel_manager
    app.state.tunnel_manager = tunnel_manager
    app.state.public_status_monitor = public_status_monitor
    image_output_dir = Path(cfg.generated_dir) if cfg.generated_dir else (Path(__file__).resolve().parent / "generated")
    file_store = FileStore(image_output_dir)
    response_store = ResponseStore(file_store=file_store, directory=cfg.response_store_dir,
                                   ttl_seconds=cfg.response_store_ttl_seconds)
    app.state.image_output_dir = image_output_dir
    app.state.file_store = file_store
    app.state.response_store = response_store
    callback_origin = f"http://{cb_host_url}:{cfg.port}"

    def current_principal() -> RequestPrincipal:
        return _principal_var.get() or RequestPrincipal()

    def current_public_base_url() -> str:
        return _public_base_url_var.get() or callback_origin

    def _unscope_session_header(scoped_sid: Optional[str], principal_obj: RequestPrincipal) -> Optional[str]:
        if not scoped_sid:
            return None
        if principal_obj.is_host:
            return scoped_sid
        prefix = f"{principal_obj.tenant_id}::"
        return scoped_sid[len(prefix):] if scoped_sid.startswith(prefix) else scoped_sid

    class _StreamPermitLease:
        def __init__(self, principal_obj: RequestPrincipal, model_req: Optional[str]) -> None:
            self._principal = principal_obj
            self._model = model_req
            self._cm = None
            self._entered = False
            self._prev_held = False

        async def enter(self) -> "_StreamPermitLease":
            if _permit_held_var.get():
                return self
            self._cm = distribution.acquire_execution_permit(
                self._principal,
                requested_model=self._model,
                capacity_provider=_effective_pool_capacity,
                queue_timeout=cfg.queue_timeout_seconds,
            )
            await self._cm.__aenter__()
            self._prev_held = _permit_held_var.get()
            _permit_held_var.set(True)
            self._entered = True
            return self

        async def close(self) -> None:
            if not self._entered or self._cm is None:
                return
            self._entered = False
            _permit_held_var.set(self._prev_held)
            await self._cm.__aexit__(None, None, None)

    @asynccontextmanager
    async def acquire_request_permit(
        principal_obj: RequestPrincipal,
        requested_model: Optional[str] = None,
    ) -> AsyncIterator[None]:
        lease = _StreamPermitLease(principal_obj, requested_model)
        await lease.enter()
        try:
            yield
        finally:
            await lease.close()

    def _record_request_with_tenant(
        principal_obj: RequestPrincipal,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        t_label = "本机直连" if principal_obj.is_host else f"客户端 · {principal_obj.name}"
        clean_sid = _unscope_session_header(kwargs.pop("session_id", None), principal_obj) or ""
        rec = usage_ledger.record_request(
            tenant_id=principal_obj.tenant_id,
            tenant_name=t_label,
            client_ip=principal_obj.client_ip,
            session_id=clean_sid,
            **kwargs,
        )
        distribution.record_usage(
            principal_obj,
            prompt_tokens=int(kwargs.get("prompt_tokens") or 0),
            completion_tokens=int(kwargs.get("completion_tokens") or 0),
            cost_usd=float(rec.get("cached_equivalent_cost_usd") or 0.0),
            model=str(kwargs.get("requested_model") or ""),
            session_id=clean_sid,
            error=False,
        )
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                hub.broadcast_ws_event({"type": "usage_update", "usage_snapshot": build_live_ledger_snapshot()})
            )
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        except RuntimeError:
            pass
        return rec

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

    def _extract_messages_preview(messages: Sequence[ChatMessage]) -> str:
        for msg in reversed(list(messages or ())):
            role = (msg.role or "").strip().lower()
            if role == "user":
                raw = msg.text_content().strip()
                if not raw:
                    continue
                title = extract_dialogue_title([("user", raw)])
                if title:
                    return title[:140]
                cleaned = re.sub(r"\s+", " ", raw).strip()
                return cleaned[:140]
            if role == "tool":
                t_name = (msg.name or "工具").strip()
                raw_t = re.sub(r"\s+", " ", msg.text_content().strip())
                return f"[{t_name} 返回结果] {raw_t[:110]}"
        return ""

    def _extract_reply_preview(content: Optional[str], tool_calls: Optional[Sequence[Dict[str, Any]]] = None) -> str:
        parts: list[str] = []
        if tool_calls:
            names = [
                str((tc.get("function") or {}).get("name") or "tool")
                for tc in tool_calls[:3]
                if isinstance(tc, dict)
            ]
            suffix = "..." if len(tool_calls) > 3 else ""
            parts.append(f"[调用 {len(tool_calls)} 项工具: {', '.join(names)}{suffix}]")
        txt = re.sub(r"\s+", " ", (content or "").strip())
        if txt:
            parts.append(txt[:150])
        return " ".join(parts)[:180]

    def _should_audit_rejected_path(path: str) -> bool:
        p = (path or "").lower()
        if not p:
            return False
        return any(
            p.endswith(seg) or seg in p
            for seg in ("/chat/completions", "/responses", "/images/", "/videos/", "/audio/")
        )

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
    async def request_trace_and_pna_middleware(request: Request, call_next: Any) -> Any:
        req_id = (request.headers.get("x-request-id") or uuid.uuid4().hex[:10]).strip()[:32]
        tok = request_id_var.set(req_id)
        started = time.monotonic()
        path = request.url.path
        is_noisy_poll = path in {"/api/bridge/poll", "/api/status", "/health"}
        if not is_noisy_poll:
            logger.debug(
                "HTTP -> %s %s client=%s",
                request.method,
                path,
                request.client.host if request.client else "local",
            )
        try:
            response = await call_next(request)
        except Exception:
            dur_ms = int((time.monotonic() - started) * 1000)
            logger.exception("HTTP !! %s %s unhandled exception after %dms", request.method, path, dur_ms)
            request_id_var.reset(tok)
            raise
        dur_ms = int((time.monotonic() - started) * 1000)
        response.headers["X-Request-Id"] = req_id
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        if response.status_code >= 400:
            logger.warning(
                "HTTP <- %s %s status=%d duration=%dms",
                request.method,
                path,
                response.status_code,
                dur_ms,
            )
        elif not is_noisy_poll:
            logger.debug(
                "HTTP <- %s %s status=%d duration=%dms",
                request.method,
                path,
                response.status_code,
                dur_ms,
            )
        request_id_var.reset(tok)
        return response

    async def verify_api_key(
        http_request: Request,
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> RequestPrincipal:
        principal = distribution.authenticate_request(
            http_request,
            master_api_key=cfg.api_key,
            authorization=authorization,
            x_api_key=x_api_key,
        )
        _principal_var.set(principal)
        _public_base_url_var.set(
            resolve_public_base_url(http_request, distribution.public_base_url, callback_origin)
        )
        return principal

    async def require_local_admin(http_request: Request) -> None:
        if not is_direct_local_connection(http_request):
            raise DistributionAuthError(
                "主机管理控制台与内部桥接端点仅限本机直连访问，已拦截外部代理或隧道请求。",
                status_code=403,
                code="local_admin_only",
            )
        caller_tok = extract_bearer_or_api_key(
            http_request.headers.get("authorization"),
            http_request.headers.get("x-api-key"),
        )
        if caller_tok.startswith("sk-geb-"):
            raise DistributionAuthError(
                "客户端共享密钥无权访问主机管理端点。",
                status_code=403,
                code="client_key_forbidden_on_admin",
            )

    @app.exception_handler(DistributionAuthError)
    async def distribution_auth_exception_handler(req: Request, exc: DistributionAuthError) -> JSONResponse:
        if _should_audit_rejected_path(req.url.path):
            p_obj = _principal_var.get() or getattr(exc, "principal", None)
            if p_obj is not None and not p_obj.is_host:
                distribution.record_usage(p_obj, error=True)
            t_id = p_obj.tenant_id if p_obj else "external_unauth"
            t_name = ("本机直连" if p_obj.is_host else f"客户端 · {p_obj.name}") if p_obj else "未授权外部请求"
            usage_ledger.record_failed_request(
                task_type="responses" if "/responses" in req.url.path else "chat",
                tenant_id=t_id,
                tenant_name=t_name,
                client_ip=p_obj.client_ip if p_obj else extract_client_ip(req),
                status="forbidden",
                status_code=exc.status_code,
                error_code=exc.code,
                error_message=exc.message,
            )
        return _openai_error_response(
            status_code=exc.status_code,
            message=exc.message,
            error_type="authentication_error" if exc.status_code == 401 else "permission_error",
            code=exc.code,
        )

    @app.exception_handler(DistributionQuotaError)
    async def distribution_quota_exception_handler(req: Request, exc: DistributionQuotaError) -> JSONResponse:
        if _should_audit_rejected_path(req.url.path):
            p_obj = _principal_var.get() or RequestPrincipal()
            t_name = "本机直连" if p_obj.is_host else f"客户端 · {p_obj.name}"
            distribution.record_usage(p_obj, error=True)
            usage_ledger.record_failed_request(
                task_type="responses" if "/responses" in req.url.path else "chat",
                tenant_id=p_obj.tenant_id,
                tenant_name=t_name,
                client_ip=p_obj.client_ip,
                status="rate_limited",
                status_code=exc.status_code,
                error_code=exc.code,
                error_message=exc.message,
            )
        hdrs = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return _openai_error_response(
            status_code=exc.status_code,
            message=exc.message,
            error_type="rate_limit_error",
            code=exc.code,
            headers=hdrs,
        )

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

    @app.get("/", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_local_admin)])
    async def root_dashboard() -> HTMLResponse:
        return HTMLResponse(content=get_dashboard_html())

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": updater.current_version,
            "browser_connected": hub.has_ready_client(),
            "gcloud_configured": gcloud.is_configured(),
            "backend_mode": cfg.backend_mode,
            "log_level": LOG_MANAGER.current_level,
        }

    @app.get("/status", response_class=HTMLResponse, include_in_schema=False)
    async def public_status_page() -> HTMLResponse:
        return HTMLResponse(
            content=get_public_status_html(),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.api_route("/status/ping", methods=["GET", "HEAD"], include_in_schema=False)
    async def public_status_ping(http_request: Request) -> JSONResponse:
        if not is_direct_local_connection(http_request):
            req_orig = resolve_public_base_url(http_request, distribution.public_base_url, callback_origin)
            tunnel_manager.note_verified_external_traffic(req_orig)
        online, busy = _public_gateway_health()
        code = 200 if online else 503
        return JSONResponse(
            status_code=code,
            content={
                "status": "ok" if online else "offline",
                "online": online,
                "busy": busy,
                "ts": int(time.time()),
            },
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
            },
        )

    def _resolve_effective_public_origin(http_request: Request) -> str:
        tun_snap = tunnel_manager.snapshot()
        active_tun_url = str(tun_snap.get("active_url") or "").strip() if tun_snap.get("running") else ""
        req_origin = resolve_public_base_url(http_request, distribution.public_base_url, callback_origin)
        if is_direct_local_connection(http_request) and (active_tun_url or distribution.public_base_url):
            return active_tun_url or distribution.public_base_url
        return req_origin

    @app.get("/api/public/status", include_in_schema=False)
    async def api_public_status(http_request: Request) -> JSONResponse:
        online, busy = _public_gateway_health()
        is_local_req = is_direct_local_connection(http_request)
        eff_origin = _resolve_effective_public_origin(http_request)
        if not is_local_req:
            tunnel_manager.note_verified_external_traffic(eff_origin)
        tun_snap = tunnel_manager.snapshot()
        last_probe = tun_snap.get("last_probe") if isinstance(tun_snap.get("last_probe"), dict) else None
        probe_matches_origin = bool(
            last_probe
            and eff_origin
            and str(last_probe.get("public_url") or "").rstrip("/") == eff_origin.rstrip("/")
        )
        srv_probe_ms = (
            int(last_probe["latency_ms"])
            if (probe_matches_origin and last_probe.get("health_ok") and last_probe.get("latency_ms"))
            else None
        )
        local_only_view = bool(
            is_local_req and not tun_snap.get("running") and not (distribution.public_base_url or "").strip()
        )
        tunnel_probe_failed = bool(
            is_local_req and probe_matches_origin and (last_probe.get("health_ok") is False)
        )
        payload = public_status_monitor.build_public_snapshot(
            online=online,
            busy=busy,
            public_origin=eff_origin,
            usage_ledger=usage_ledger,
            server_probe_ms=srv_probe_ms,
            local_only_view=local_only_view,
            tunnel_probe_failed=tunnel_probe_failed,
        )
        return JSONResponse(
            status_code=200,
            content=payload,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.post("/api/public/network-map", include_in_schema=False)
    async def api_public_network_map(http_request: Request) -> JSONResponse:
        eff_origin = _resolve_effective_public_origin(http_request)
        # Only allow force-bypassing the 90s cache from local host admin
        allow_force = is_direct_local_connection(http_request) and (
            http_request.query_params.get("force") == "1"
        )
        try:
            res = await public_status_monitor.run_network_map_probe(eff_origin, force=allow_force)
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "detail": f"分布式硬件节点拨测暂时繁忙：{exc}"},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            status_code=200 if res.get("ok") else 400,
            content=res,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/api/status", dependencies=[Depends(require_local_admin)])
    async def api_status() -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        if hub_snap.get("connected_extension_version"):
            updater.record_extension_version(str(hub_snap["connected_extension_version"]))
        upd_snap = updater.status_snapshot(include_remote_if_cached=True)
        live_ledger = build_live_ledger_snapshot(include_catalog=False, hub_snap=hub_snap)
        oob_compactions = context_builder.find_out_of_band_superseded_compactions()
        if oob_compactions:
            schedule_compaction_resets(oob_compactions)
        ctx_mon = live_ledger.get("context_monitor") or {}
        ctx_slots = ctx_mon.get("slots") or []
        working_tab_ids = [
            int(s["tab_id"])
            for s in ctx_slots
            if isinstance(s, dict)
            and s.get("tab_id") is not None
            and (bool(s.get("busy")) or bool(s.get("session_active")) or int(s.get("waiting_count") or 0) > 0)
        ]
        hub_snap["working_slots"] = int(ctx_mon.get("working_session_count") or 0)
        await hub.push_working_tabs_if_changed(working_tab_ids)
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
            "default_model": cfg.default_model,
            "api_key_required": bool(cfg.api_key),
            "logging": LOG_MANAGER.status(),
            "context": {"carbon_model_capacity": CARBON_CONTEXT_WINDOW,
                        "carbon_checkpoint_budget": CARBON_AUTO_COMPACT_TOKENS,
                        "checkpoint_policy": "Local coding stability budget; Enterprise web threshold unpublished",
                        "token_count_source": "local_estimate_not_google_usage",
                        "last_request": dict(context_diagnostics)},
            "gcloud_configured": gcloud.is_configured(),
            "hub": hub_snap,
            "update": upd_snap,
            "usage": live_ledger["usage"],
            "context_monitor": live_ledger["context_monitor"],
            "distribution": live_ledger["distribution"],
            "tunnel": live_ledger["tunnel"],
            "sessions_overview": live_ledger["sessions_overview"],
        }

    @app.get("/api/logs", dependencies=[Depends(require_local_admin)])
    async def api_get_logs(
        limit: int = 120,
        level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "status": "ok",
            "logging": LOG_MANAGER.status(),
            "server_logs": LOG_MANAGER.get_logs(limit=limit, min_level=level, search=search),
            "browser_logs": hub._recent_logs[-max(1, min(limit, 250)) :],
        }

    @app.post("/api/logs/level", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_set_log_level(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "请求体格式错误"})
        new_level = str(body.get("level") or "INFO").strip().upper()
        log_payloads = body.get("log_payloads") if "log_payloads" in body else None
        updated = LOG_MANAGER.set_level(new_level, log_payloads=log_payloads)
        logger.info("日志调试级别已动态调整为: %s (log_payloads=%s)", updated["level"], updated["log_payloads"])
        return JSONResponse(status_code=200, content={"ok": True, "logging": updated})

    @app.get("/api/update/status", dependencies=[Depends(require_local_admin)])
    async def api_update_status(force: bool = False) -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        if hub_snap.get("connected_extension_version"):
            updater.record_extension_version(str(hub_snap["connected_extension_version"]))
        info = await asyncio.to_thread(updater.check_github_update, force)
        return {"status": "ok", **info}

    @app.post("/api/update/extension", dependencies=[Depends(require_local_admin)])
    async def api_update_extension() -> JSONResponse:
        try:
            result = await asyncio.to_thread(updater.update_extension_only)
        except UpdateIntegrityError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "code": "integrity_error"})
        except UpdateError as exc:
            return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "update_failed"})
        updated_ver = str(result.get("updated_version") or updater.current_version)
        await hub.broadcast_ws_event(
            {"type": "extension_reload_available", "version": updated_ver},
            required_capability="auto_update",
        )
        return JSONResponse(status_code=200, content=result)

    @app.post("/api/update/apply", dependencies=[Depends(require_local_admin)])
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
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "code": "integrity_error"})
        except UpdateError as exc:
            return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "update_failed"})
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

    @app.get("/api/usage", dependencies=[Depends(require_local_admin)])
    async def api_usage() -> Dict[str, Any]:
        live_ledger = build_live_ledger_snapshot(include_catalog=True)
        return {
            "status": "ok",
            "usage": live_ledger["usage"],
            "context_monitor": live_ledger["context_monitor"],
            "distribution": live_ledger["distribution"],
            "tunnel": live_ledger["tunnel"],
            "sessions_overview": live_ledger["sessions_overview"],
        }

    @app.post("/api/slots/reset", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_reset_worker_slot(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        slot_key = str((body or {}).get("slot_key") or "").strip()
        if not slot_key:
            return JSONResponse(status_code=400, content={"ok": False, "detail": "缺少席位标识 slot_key"})
        existing_slot = hub._slots.get(slot_key)
        if existing_slot is not None:
            if (
                existing_slot.is_busy
                or existing_slot.waiting_count > 0
                or context_builder.is_slot_session_active(slot_key, existing_slot.to_public_dict())
            ):
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "detail": "该工作席位正在执行任务中，请等待本轮任务完成后再开启新对话",
                    },
                )
            context_builder.evict_slot_to_archive(
                slot_key,
                fallback_url=existing_slot.tab_url or "",
                fallback_explicit_session_id=existing_slot.pinned_session_id,
            )
        try:
            result = await hub.reset_worker_slot(
                slot_key,
                on_reset_locked=lambda: context_builder.invalidate_session(slot_key),
            )
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"ok": False, "detail": str(exc)})
        except RuntimeError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
        context_builder.invalidate_session(slot_key)
        return JSONResponse(status_code=200, content=result)

    @app.post("/api/extension/reload", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_reload_extension() -> JSONResponse:
        try:
            result = await hub.reload_connected_extensions()
        except RuntimeError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
        return JSONResponse(status_code=200, content=result)

    @app.post("/api/seats/limit", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_set_seats_limit(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_limit = (body or {}).get("max_parallel_tabs")
        if raw_limit is None:
            return JSONResponse(status_code=400, content={"ok": False, "detail": "缺少参数 max_parallel_tabs"})
        if isinstance(raw_limit, bool):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "max_parallel_tabs 必须为 1~200 的整数"})
        try:
            if isinstance(raw_limit, str):
                trimmed = raw_limit.strip()
                if not trimmed or not trimmed.lstrip("-").isdigit():
                    raise ValueError("not integer")
                parsed = int(trimmed)
            elif isinstance(raw_limit, int):
                parsed = raw_limit
            elif isinstance(raw_limit, float) and raw_limit.is_integer():
                parsed = int(raw_limit)
            else:
                raise ValueError("not integer")
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "max_parallel_tabs 必须为 1~200 的整数"})
        if parsed < 1 or parsed > 200:
            return JSONResponse(status_code=400, content={"ok": False, "detail": "席位上限范围应为 1~200"})
        result = await hub.set_max_parallel_tabs(parsed)
        return JSONResponse(status_code=200, content={**result, "hub": hub.status_snapshot()})

    # ------------------------------------------------------------------
    # Public API Distribution & Multi-Client Admin Endpoints (Local Only)
    # ------------------------------------------------------------------

    @app.get("/api/distribution", dependencies=[Depends(require_local_admin)])
    async def api_get_distribution() -> Dict[str, Any]:
        return {
            "status": "ok",
            "tunnel": tunnel_manager.snapshot(),
            **distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
        }

    @app.get("/api/tunnel", dependencies=[Depends(require_local_admin)])
    async def api_get_tunnel() -> Dict[str, Any]:
        return {
            "ok": True,
            "tunnel": tunnel_manager.snapshot(),
            "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
        }

    @app.post("/api/tunnel/start", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_start_tunnel(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            snap = await tunnel_manager.start(
                mode=str(body.get("mode") or "quick") if "mode" in body else None,
                named_tunnel_token=str(body.get("named_tunnel_token") or "") if "named_tunnel_token" in body else None,
                named_public_url=str(body.get("named_public_url") or "") if "named_public_url" in body else None,
                auto_sync_public_url=bool(body["auto_sync_public_url"]) if "auto_sync_public_url" in body else None,
                restart=bool(body.get("restart", False)),
            )
        except (ValueError, RuntimeError) as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "detail": str(exc),
                    "tunnel": tunnel_manager.snapshot(),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "tunnel": snap,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/tunnel/stop", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_stop_tunnel(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        clear_url = bool((body or {}).get("clear_public_url", False)) if isinstance(body, dict) else False
        snap = await asyncio.to_thread(tunnel_manager.stop, clear_public_url=clear_url)
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "tunnel": snap,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/tunnel/probe", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_probe_tunnel(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        pub_url = str(body.get("public_url") or "").strip() or distribution.public_base_url
        sample_key = str(body.get("sample_api_key") or "").strip() or None
        if not sample_key:
            dist_snap = distribution.snapshot(include_secrets=True)
            for c_item in dist_snap.get("clients") or []:
                if c_item.get("enabled") and not c_item.get("expired") and c_item.get("api_key"):
                    sample_key = str(c_item["api_key"])
                    break
        try:
            probe_res = await tunnel_manager.probe_and_recover(
                pub_url or None,
                sample_api_key=sample_key,
                can_restart=lambda: not any(
                    hub.status_snapshot().get(key)
                    for key in ("in_flight_requests", "queued_requests")
                ),
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "ok": bool(probe_res.get("ok")),
                "probe": probe_res,
                "tunnel": tunnel_manager.snapshot(),
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.get("/api/sessions", dependencies=[Depends(require_local_admin)])
    async def api_list_sessions(tenant_id: Optional[str] = None) -> Dict[str, Any]:
        hub_snap = hub.status_snapshot()
        items = context_builder.list_sessions_overview(
            hub_snap.get("worker_slots") or [],
            tenant_label_fn=distribution.get_tenant_label,
            limit=150,
        )
        if tenant_id and tenant_id.strip():
            items = [x for x in items if x.get("tenant_id") == tenant_id.strip()]
        return {"ok": True, "sessions": items}

    @app.post("/api/sessions/delete", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_delete_session(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "请求格式错误"})
        session_ref = str(body.get("session_ref") or "").strip()
        tenant_purge = str(body.get("purge_tenant_id") or "").strip()
        if tenant_purge:
            ev_res = hub.evict_tenant_slots(tenant_purge, abort_inflight=bool(body.get("abort_inflight", False)))
            pg_res = context_builder.purge_tenant_sessions(tenant_purge, include_archive=True)
            return JSONResponse(status_code=200, content={"ok": True, **ev_res, **pg_res})
        if session_ref.startswith("archive:"):
            akey = session_ref[len("archive:"):]
            context_builder.delete_archived_session(akey)
            return JSONResponse(status_code=200, content={"ok": True, "deleted": session_ref})
        if session_ref.startswith("slot:"):
            skey = session_ref[len("slot:"):]
            slot_obj = hub._slots.get(skey)
            if slot_obj is not None and slot_obj.is_busy and not body.get("force"):
                return JSONResponse(status_code=409, content={"ok": False, "detail": "该席位正在执行任务中，请稍后再试或使用强制中断。"})
            try:
                await hub.reset_worker_slot(
                    skey,
                    on_reset_locked=lambda: context_builder.invalidate_session(skey),
                )
            except KeyError:
                context_builder.invalidate_session(skey)
            except RuntimeError as exc:
                return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
            context_builder.invalidate_session(skey)
            return JSONResponse(status_code=200, content={"ok": True, "deleted": session_ref})
        return JSONResponse(status_code=400, content={"ok": False, "detail": "缺少有效的 session_ref"})
    @app.patch("/api/distribution/policy", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    @app.post("/api/distribution/policy", dependencies=[Depends(require_local_admin), Depends(verify_api_key)], include_in_schema=False)
    async def api_update_distribution_policy(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "请求体格式错误"})
        distribution.update_policy(
            external_access_enabled=body.get("external_access_enabled") if "external_access_enabled" in body else None,
            host_reserved_slots=body.get("host_reserved_slots") if "host_reserved_slots" in body else None,
            external_queue_timeout_seconds=body.get("external_queue_timeout_seconds") if "external_queue_timeout_seconds" in body else None,
            public_base_url=body.get("public_base_url") if "public_base_url" in body else None,
        )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/distribution/clients", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_create_distribution_client(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "请求体格式错误"})
        raw_models = body.get("allowed_models")
        if isinstance(raw_models, str):
            allowed_models = [m.strip() for m in raw_models.split(",") if m.strip()]
        elif isinstance(raw_models, list):
            allowed_models = [str(m).strip() for m in raw_models if str(m).strip()]
        else:
            allowed_models = []
        raw_ips = body.get("allowed_ips")
        if isinstance(raw_ips, str):
            allowed_ips = [ip.strip() for ip in raw_ips.split(",") if ip.strip()]
        elif isinstance(raw_ips, list):
            allowed_ips = [str(ip).strip() for ip in raw_ips if str(ip).strip()]
        else:
            allowed_ips = []
        try:
            created = distribution.create_client(
                name=str(body.get("name") or ""),
                note=str(body.get("note") or ""),
                max_concurrency=int(body.get("max_concurrency") if body.get("max_concurrency") is not None else 2),
                rpm_limit=int(body.get("rpm_limit") if body.get("rpm_limit") is not None else 30),
                daily_request_limit=int(body.get("daily_request_limit") or 0),
                daily_token_limit=int(body.get("daily_token_limit") or 0),
                daily_cost_limit_usd=float(body.get("daily_cost_limit_usd") or 0.0),
                total_token_limit=int(body.get("total_token_limit") or 0),
                expires_at=int(body.get("expires_at") or 0),
                allowed_models=allowed_models,
                allowed_ips=allowed_ips,
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "client": created,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.patch("/api/distribution/clients/{client_id}", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_update_distribution_client(client_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "请求体格式错误"})
        allowed_models: Optional[list[str]] = None
        if "allowed_models" in body:
            raw_models = body.get("allowed_models")
            if isinstance(raw_models, str):
                allowed_models = [m.strip() for m in raw_models.split(",") if m.strip()]
            elif isinstance(raw_models, list):
                allowed_models = [str(m).strip() for m in raw_models if str(m).strip()]
            else:
                allowed_models = []
        allowed_ips: Optional[list[str]] = None
        if "allowed_ips" in body:
            raw_ips = body.get("allowed_ips")
            if isinstance(raw_ips, str):
                allowed_ips = [ip.strip() for ip in raw_ips.split(",") if ip.strip()]
            elif isinstance(raw_ips, list):
                allowed_ips = [str(ip).strip() for ip in raw_ips if str(ip).strip()]
            else:
                allowed_ips = []
        try:
            updated = distribution.update_client(
                client_id,
                name=str(body["name"]) if "name" in body and body["name"] is not None else None,
                note=str(body["note"]) if "note" in body and body["note"] is not None else None,
                enabled=bool(body["enabled"]) if "enabled" in body and body["enabled"] is not None else None,
                max_concurrency=int(body["max_concurrency"]) if "max_concurrency" in body and body["max_concurrency"] is not None else None,
                rpm_limit=int(body["rpm_limit"]) if "rpm_limit" in body and body["rpm_limit"] is not None else None,
                daily_request_limit=int(body["daily_request_limit"]) if "daily_request_limit" in body and body["daily_request_limit"] is not None else None,
                daily_token_limit=int(body["daily_token_limit"]) if "daily_token_limit" in body and body["daily_token_limit"] is not None else None,
                daily_cost_limit_usd=float(body["daily_cost_limit_usd"]) if "daily_cost_limit_usd" in body and body["daily_cost_limit_usd"] is not None else None,
                total_token_limit=int(body["total_token_limit"]) if "total_token_limit" in body and body["total_token_limit"] is not None else None,
                expires_at=int(body["expires_at"]) if "expires_at" in body and body["expires_at"] is not None else None,
                allowed_models=allowed_models,
                allowed_ips=allowed_ips,
            )
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"ok": False, "detail": str(exc)})
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
        if body.get("enabled") is False:
            await distribution.abort_client_waiters(client_id)
            hub.evict_tenant_slots(f"client:{client_id}", abort_inflight=bool(body.get("abort_inflight", False)))
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "client": updated,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/distribution/clients/{client_id}/rotate-key", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_rotate_distribution_client_key(client_id: str) -> JSONResponse:
        try:
            rotated = distribution.rotate_client_key(client_id)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"ok": False, "detail": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "client": rotated,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/distribution/clients/{client_id}/reset-quota", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_reset_distribution_client_quota(client_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        reset_total = bool((body or {}).get("reset_total", False)) if isinstance(body, dict) else False
        try:
            updated = distribution.reset_client_quota(client_id, reset_total=reset_total)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"ok": False, "detail": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "client": updated,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.post("/api/distribution/clients/{client_id}/disconnect", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_disconnect_distribution_client(client_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        purge_archive = bool((body or {}).get("purge_archive", False)) if isinstance(body, dict) else False
        aborted_waiters = await distribution.abort_client_waiters(client_id)
        tenant_tag = f"client:{client_id}"
        ev_res = hub.evict_tenant_slots(tenant_tag, abort_inflight=True)
        pg_res = context_builder.purge_tenant_sessions(tenant_tag, include_archive=purge_archive)
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "aborted_waiters": aborted_waiters,
                **ev_res,
                **pg_res,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    @app.delete("/api/distribution/clients/{client_id}", dependencies=[Depends(require_local_admin), Depends(verify_api_key)])
    async def api_delete_distribution_client(client_id: str) -> JSONResponse:
        if not distribution.delete_client(client_id):
            return JSONResponse(status_code=404, content={"ok": False, "detail": f"未找到客户端：{client_id}"})
        await distribution.abort_client_waiters(client_id)
        tenant_tag = f"client:{client_id}"
        hub.evict_tenant_slots(tenant_tag, abort_inflight=True)
        context_builder.purge_tenant_sessions(tenant_tag, include_archive=True)
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "deleted_client_id": client_id,
                "distribution": distribution.snapshot(include_secrets=True, pool_capacity=_effective_pool_capacity()),
            },
        )

    # ------------------------------------------------------------------
    # Browser Extension WebSocket Endpoint
    # ------------------------------------------------------------------

    @app.websocket("/ws/browser")
    async def websocket_browser_endpoint(websocket: WebSocket) -> None:
        if not is_direct_local_connection(websocket):
            await websocket.close(code=1008, reason="Local loopback connection required")
            return
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
                                {"type": "extension_reload_available", "version": str(reload_ver)}
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

    @app.post("/api/bridge/register", dependencies=[Depends(require_local_admin)])
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

    @app.get("/api/bridge/poll", dependencies=[Depends(require_local_admin)])
    async def poll_http_bridge(client_id: str, timeout: float = 18.0) -> Dict[str, Any]:
        bounded = max(1.0, min(timeout, 25.0))
        command = await hub.poll_http_command(client_id=client_id, timeout=bounded)
        return {"ok": True, "command": command}

    @app.post("/api/bridge/event", dependencies=[Depends(require_local_admin)])
    async def receive_http_bridge_event(payload: BridgeEventPayload) -> Dict[str, Any]:
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        raw["type"] = payload.event
        ack = await hub.handle_browser_event(payload.client_id, raw)
        return {"ok": True, "ack": ack}

    @app.post("/api/bridge/callback", dependencies=[Depends(require_local_admin)])
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
                    "tab_url": payload.tab_url,
                    "tab_title": payload.tab_title,
                    "reply": payload.reply or "",
                    "output_images": payload.output_images or [],
                    "output_files": payload.output_files or [],
                    "citations": payload.citations or [],
                    "source": payload.source or "http_callback",
                    "active_model": payload.active_model,
                },
            )
        return {"ok": True}

    @app.get("/api/bridge/attachments/{attachment_id}", dependencies=[Depends(require_local_admin)])
    async def fetch_staged_bridge_attachment(attachment_id: str, token: str = "") -> Response:
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
        return {
            "object": "list",
            "data": distribution.filter_models_for_principal(current_principal(), AVAILABLE_MODELS),
        }

    @app.get("/v1/models/{model_id:path}", dependencies=[Depends(verify_api_key)])
    @app.get("/models/{model_id:path}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def retrieve_model(model_id: str) -> Any:
        clean_id = (model_id or "").strip().strip("/")
        if not clean_id:
            return {
                "object": "list",
                "data": distribution.filter_models_for_principal(current_principal(), AVAILABLE_MODELS),
            }
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
        principal = current_principal()
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
                    tenant_id=principal.tenant_id,
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
                    tenant_id=principal.tenant_id,
                )
            return {k: v for k, v in meta.items() if k not in {"path", "tenant_id"}}
        except (InvalidAttachmentError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "error_code", "invalid_file_upload")
            return _openai_error_response(400, str(exc), "invalid_request_error", code)

    @app.get("/v1/files", dependencies=[Depends(verify_api_key)])
    @app.get("/files", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def list_uploaded_files(purpose: Optional[str] = None) -> Dict[str, Any]:
        return {
            "object": "list",
            "data": file_store.list_files(purpose=purpose, tenant_id=current_principal().tenant_id),
        }

    @app.get("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
    @app.get("/files/{file_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def retrieve_uploaded_file(file_id: str) -> Any:
        try:
            meta = file_store.get_meta(file_id, tenant_id=current_principal().tenant_id)
            return {k: v for k, v in meta.items() if k not in {"path", "tenant_id"}}
        except FileNotFoundError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "file_not_found")

    @app.get("/v1/files/{file_id}/content", dependencies=[Depends(verify_api_key)])
    @app.get("/files/{file_id}/content", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def download_uploaded_file(file_id: str) -> Any:
        try:
            meta = file_store.get_meta(file_id, tenant_id=current_principal().tenant_id)
            return FileResponse(meta["path"], media_type=meta["mime_type"], filename=meta["filename"])
        except FileNotFoundError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "file_not_found")

    @app.delete("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
    @app.delete("/files/{file_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def delete_uploaded_file(file_id: str) -> Any:
        if not file_store.delete(file_id, tenant_id=current_principal().tenant_id):
            return _openai_error_response(404, f"文件不存在：{file_id}", "invalid_request_error", "file_not_found")
        return {"id": file_id, "object": "file", "deleted": True}

    async def _run_tracked_image_gen(
        img_payload: Dict[str, Any],
        img_attachments: list[dict[str, Any]],
        explicit_task: Optional[str] = None,
    ) -> Dict[str, Any]:
        principal = current_principal()
        pub_base = current_public_base_url()
        img_model = normalize_default_model(img_payload.get("model"), cfg.default_model)
        img_prompt_prev = str(img_payload.get("prompt") or "").strip()[:140]
        t_kind = explicit_task or ("image_edit" if img_attachments else "image_gen")
        task_id = f"img_{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        distribution.register_live_task(
            task_id,
            principal,
            model=img_model,
            task_type=t_kind,
            prompt_preview=img_prompt_prev,
            state="queued",
        )
        try:
            async with acquire_request_permit(principal, img_model):
                distribution.update_live_task(task_id, state="running")
                res = await generate_images(
                    img_payload,
                    hub,
                    context_builder,
                    cfg,
                    image_output_dir,
                    img_attachments,
                    file_store=file_store,
                    tenant_id=principal.tenant_id,
                    public_base_url=pub_base,
                )
        finally:
            distribution.finish_live_task(task_id)
        p_toks = estimate_tokens(str(img_payload.get("prompt") or "")) + estimate_attachment_input_tokens(img_attachments)
        _record_request_with_tenant(
            principal,
            request_id=task_id,
            requested_model=img_model,
            prompt_tokens=p_toks,
            completion_tokens=0,
            browser_active_model=resolve_target_web_model(img_model),
            task_type=t_kind,
            strategy="browser_image",
            images_generated=max(1, len(res.get("data") or [])),
            prompt_preview=img_prompt_prev,
            reply_preview=f"[生成图片 {max(1, len(res.get('data') or []))} 张]",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return res

    @app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
    async def image_generations(payload: Dict[str, Any]) -> Any:
        principal = current_principal()
        try:
            images = payload.get("images") or []
            if not isinstance(images, list):
                raise ValueError("images 必须是图片 data URL 或 file_id 数组。")
            attachments = collect_attachments(
                [ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": image}} for image in images])],
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
                tenant_id=principal.tenant_id,
            )
            return await _run_tracked_image_gen(payload, attachments)
        except (InvalidAttachmentError, ValueError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_image_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            distribution.record_usage(principal, error=True)
            return _openai_error_response(502, str(exc), "bridge_error", "image_generation_failed")

    @app.post("/v1/images/edits", dependencies=[Depends(verify_api_key)])
    async def image_edits(raw_request: Request) -> Any:
        principal = current_principal()
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
                        tenant_id=principal.tenant_id,
                    )
                    parts.append({"type": "image_url", "image_url": {"url": saved["id"]}})
                payload = {
                    "prompt": fields.get("prompt", ""),
                    "model": normalize_default_model(fields.get("model"), cfg.default_model),
                    "n": int(fields.get("n", "1") or "1"),
                    "size": fields.get("size", "auto"),
                    "response_format": fields.get("response_format", "b64_json"),
                }
                attachments = collect_attachments(
                    [ChatMessage(role="user", content=parts)],
                    file_store=file_store,
                    callback_base_url=callback_origin,
                    image_output_dir=image_output_dir,
                    tenant_id=principal.tenant_id,
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
                    tenant_id=principal.tenant_id,
                )
            return await _run_tracked_image_gen(payload, attachments, explicit_task="image_edit")
        except (InvalidAttachmentError, ValueError, json.JSONDecodeError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_image_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            distribution.record_usage(principal, error=True)
            return _openai_error_response(502, str(exc), "bridge_error", "image_generation_failed")

    @app.get("/v1/images/files/{filename}")
    async def image_file(
        filename: str,
        http_request: Request,
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> Any:
        if authorization or x_api_key or cfg.api_key:
            await verify_api_key(http_request, authorization=authorization, x_api_key=x_api_key)
        if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|webp|gif|bmp|svg|avif|ico)", filename) or not (image_output_dir / filename).is_file():
            return _openai_error_response(404, "图片文件不存在。", "invalid_request_error", "image_not_found")
        caller_p = _principal_var.get()
        if caller_p is not None and not caller_p.is_host:
            owner_meta_file = image_output_dir / ".img_owners" / f"{filename}.json"
            img_owner = "host"
            if owner_meta_file.is_file():
                try:
                    img_owner = str(json.loads(owner_meta_file.read_text(encoding="utf-8")).get("tenant_id") or "host")
                except Exception:
                    img_owner = "host"
            if img_owner != caller_p.tenant_id:
                return _openai_error_response(404, "图片文件不存在或无权访问。", "invalid_request_error", "image_not_found")
        return FileResponse(image_output_dir / filename)

    @app.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)])
    async def video_generations(payload: Dict[str, Any]) -> Any:
        principal = current_principal()
        pub_base = current_public_base_url()
        v_model = normalize_default_model(payload.get("model"), cfg.default_model)
        try:
            raw_refs = payload.get("images") or payload.get("files") or []
            if not isinstance(raw_refs, list):
                raise ValueError("images/files 必须是数组。")
            attachments = collect_attachments(
                [ChatMessage(role="user", content=[{"type": "input_file", "file_data": ref} for ref in raw_refs])],
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
                tenant_id=principal.tenant_id,
            ) if raw_refs else []
            v_started = time.monotonic()
            v_task_id = f"vid_{uuid.uuid4().hex[:12]}"
            v_prompt_prev = str(payload.get("prompt") or "").strip()[:140]
            distribution.register_live_task(
                v_task_id,
                principal,
                model=v_model,
                task_type="video_gen",
                prompt_preview=v_prompt_prev,
                state="queued",
            )
            try:
                async with acquire_request_permit(principal, v_model):
                    distribution.update_live_task(v_task_id, state="running")
                    v_res = await generate_videos(
                        payload,
                        hub,
                        context_builder,
                        cfg,
                        image_output_dir,
                        file_store,
                        attachments,
                        tenant_id=principal.tenant_id,
                        public_base_url=pub_base,
                    )
            finally:
                distribution.finish_live_task(v_task_id)
            _record_request_with_tenant(
                principal,
                request_id=v_task_id,
                requested_model=v_model,
                prompt_tokens=estimate_tokens(str(payload.get("prompt") or "")) + estimate_attachment_input_tokens(attachments),
                completion_tokens=0,
                browser_active_model=resolve_target_web_model(v_model),
                task_type="video_gen",
                strategy="browser_video",
                videos_generated=max(1, len(v_res.get("data") or [])),
                prompt_preview=v_prompt_prev,
                reply_preview=f"[生成视频 {max(1, len(v_res.get('data') or []))} 段]",
                duration_ms=int((time.monotonic() - v_started) * 1000),
            )
            return v_res
        except (InvalidAttachmentError, ValueError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_video_request")
        except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError) as exc:
            distribution.record_usage(principal, error=True)
            return _openai_error_response(502, str(exc), "bridge_error", "video_generation_failed")

    async def _handle_audio_task(raw_request: Request, translate_to_english: bool = False) -> Any:
        principal = current_principal()
        audio_started = time.monotonic()
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
                    tenant_id=principal.tenant_id,
                )
                fid = saved["id"]
                model_req = normalize_default_model(fields.get("model"), cfg.default_model)
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
                        tenant_id=principal.tenant_id,
                    )
                    fid = saved["id"]
                model_req = normalize_default_model(payload.get("model"), cfg.default_model)
                hint = str(payload.get("prompt") or "").strip()
                lang = str(payload.get("language") or "").strip()
                resp_fmt = str(payload.get("response_format") or "json").strip().lower()

            if resp_fmt not in {"json", "text", "verbose_json", "srt", "vtt"}:
                raise ValueError("response_format 仅支持 json、text、verbose_json、srt、vtt。")

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
            attachments = collect_attachments(
                [msg],
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
                tenant_id=principal.tenant_id,
            )
            aud_task_id = f"aud_{uuid.uuid4().hex[:12]}"
            aud_preview = ("[语音翻译] " if translate_to_english else "[语音转写] ") + (hint or lang or "音频文件")
            distribution.register_live_task(
                aud_task_id,
                principal,
                model=model_req,
                task_type="audio",
                prompt_preview=aud_preview,
                state="queued",
            )
            try:
                async with acquire_request_permit(principal, model_req):
                    if not hub.has_ready_client():
                        raise BridgeUnavailableError("没有连接可用的 Gemini Enterprise 页面。")
                    aud_sid = principal.scope_session_id("audio_" + uuid.uuid4().hex)
                    async with hub.acquire_slot(
                        explicit_session_id=aud_sid,
                        queue_timeout=cfg.queue_timeout_seconds,
                        tenant_id=principal.tenant_id,
                    ) as slot:
                        distribution.update_live_task(
                            aud_task_id,
                            state="running",
                            slot_key=slot.slot_key,
                            tab_id=slot.tab_id,
                        )
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
            finally:
                distribution.finish_live_task(aud_task_id)
            cleaned_text = parse_assistant_output(raw_reply).content.strip()
            _record_request_with_tenant(
                principal,
                request_id=aud_task_id,
                requested_model=model_req,
                prompt_tokens=estimate_tokens(instruction) + estimate_attachment_input_tokens(attachments),
                completion_tokens=estimate_tokens(cleaned_text),
                browser_active_model=resolve_target_web_model(model_req),
                task_type="audio",
                strategy="audio_translate" if translate_to_english else "audio_transcribe",
                audio_requests=1,
                duration_ms=int((time.monotonic() - audio_started) * 1000),
                prompt_preview=aud_preview,
                reply_preview=cleaned_text[:150],
            )
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
            distribution.record_usage(principal, error=True)
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
        principal = current_principal()
        try:
            request, mapping, history = prepare_request(
                payload,
                response_store,
                tenant_id=principal.tenant_id,
                headers=raw_request.headers,
            )
        except (ResponsesError, ValidationError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_responses_request")
        try:
            forwarded_x_sid = None if request._bridge_thread_id else raw_request.headers.get("x-session-id")
            tok = _ledger_task_var.set("responses")
            cmp_in_tok = _consumed_compactions_var.set(
                _extract_input_compaction_tokens(payload.get("input"))
            )
            try:
                upstream = await chat_completions(
                    request, x_session_id=forwarded_x_sid,
                    x_bridge_session=raw_request.headers.get("x-bridge-session"))
            finally:
                _consumed_compactions_var.reset(cmp_in_tok)
                _ledger_task_var.reset(tok)
            def _responses_gen_images() -> list[dict[str, Any]]:
                pending = [
                    a for a in collect_attachments(
                        request.messages,
                        file_store=file_store,
                        callback_base_url=callback_origin,
                        image_output_dir=image_output_dir,
                        pending_only=True,
                        tenant_id=principal.tenant_id,
                    )
                    if a["category"] == "image"
                ]
                if pending:
                    return pending
                req_tools = normalize_request_tools(request.tools, request.functions)
                if has_coding_tools(req_tools) or has_tool_history(request.messages):
                    return []
                rebuilt, _ = collect_rebuild_attachments(
                    request.messages,
                    [],
                    file_store=file_store,
                    callback_base_url=callback_origin,
                    image_output_dir=image_output_dir,
                    tenant_id=principal.tenant_id,
                )
                return [a for a in rebuilt if a["category"] == "image"]

            eff_resp_model = normalize_default_model(request.model, cfg.default_model)
            return await translate_response(upstream, payload, eff_resp_model, mapping, history, response_store,
                image_generator=lambda prompt: _run_tracked_image_gen(
                    {"prompt": prompt, "model": eff_resp_model},
                    _responses_gen_images(),
                ),
                tenant_id=principal.tenant_id,
                headers=raw_request.headers)
        except (ResponsesError, BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError) as exc:
            return _openai_error_response(502, str(exc), "bridge_error", "response_execution_failed")

    @app.get("/v1/responses/{response_id}", dependencies=[Depends(verify_api_key)])
    async def retrieve_response(response_id: str) -> Any:
        try:
            stored = response_store.get(response_id, tenant_id=current_principal().tenant_id)[0]
            return {k: v for k, v in stored.items() if not str(k).startswith("_")}
        except ResponsesError as exc:
            return _openai_error_response(404, str(exc), "invalid_request_error", "response_not_found")

    @app.delete("/v1/responses/{response_id}", dependencies=[Depends(verify_api_key)])
    async def delete_response(response_id: str) -> Any:
        if not response_store.delete(response_id, tenant_id=current_principal().tenant_id):
            return _openai_error_response(404, "会话记录不存在。", "invalid_request_error", "response_not_found")
        return {"id": response_id, "object": "response.deleted", "deleted": True}

    @app.post("/v1/responses/input_tokens", dependencies=[Depends(verify_api_key)])
    async def response_input_tokens(payload: Dict[str, Any]) -> Any:
        try:
            request, _, _ = prepare_request(
                payload, response_store, tenant_id=current_principal().tenant_id
            )
            text = prompt_for_usage(request)
            return {"object": "response.input_tokens", "input_tokens": estimate_tokens(text)}
        except (ResponsesError, ValidationError) as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_token_request")

    @app.post("/v1/responses/compact", dependencies=[Depends(verify_api_key)])
    async def compact_response(payload: Dict[str, Any], raw_request: Request) -> Any:
        principal = current_principal()
        try:
            request, _, _ = prepare_request(
                {**payload, "tools": [], "stream": False},
                response_store,
                tenant_id=principal.tenant_id,
                headers=raw_request.headers,
            )
            request.messages, recent_tool_rounds = split_checkpoint_history(request.messages)
            request.messages.append(ChatMessage(role="user", content=COMPACTION_INSTRUCTIONS))
            cmp_tok = _ledger_task_var.set("compaction")
            rec_tok = _latest_compaction_rec_var.set(None)
            try:
                result = await chat_completions(request, x_session_id=None, x_bridge_session=None)
                comp_rec = _latest_compaction_rec_var.get()
            finally:
                _latest_compaction_rec_var.reset(rec_tok)
                _ledger_task_var.reset(cmp_tok)
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
                compact_messages_for_store(
                    [m.model_dump(exclude_none=True) for m in summaries],
                    file_store,
                    tenant_id=principal.tenant_id,
                ),
                tenant_id=principal.tenant_id,
            )
            if comp_rec is not None:
                context_builder.mark_compaction_delivered(
                    comp_rec.slot_key,
                    comp_rec.compaction_seq,
                    compaction_token=token,
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
        x_bridge_probe: Optional[str] = Header(default=None),
        x_thread_id: Optional[str] = Header(default=None),
        thread_id: Optional[str] = Header(default=None, alias="thread-id"),
    ) -> Any:
        if not request.messages:
            return _openai_error_response(
                status_code=400,
                message="messages 不能为空。",
                error_type="invalid_request_error",
                code="empty_messages",
            )

        principal = current_principal()
        pub_base_url = current_public_base_url()
        raw_model_name = normalize_default_model(request.model, cfg.default_model)
        distribution.preflight_check(principal, requested_model=raw_model_name)
        base_model_name, model_session_tag = parse_model_and_session(raw_model_name)
        model_name = normalize_default_model(base_model_name, cfg.default_model)
        clean_x_bridge = x_bridge_session.strip() if isinstance(x_bridge_session, str) and x_bridge_session.strip() else None
        clean_thread_hdr = (
            (x_thread_id.strip() if isinstance(x_thread_id, str) and x_thread_id.strip() else None)
            or (thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None)
        )
        clean_x_sid = x_session_id.strip() if isinstance(x_session_id, str) and x_session_id.strip() else None
        clean_req_sid = request.session_id.strip() if isinstance(request.session_id, str) and request.session_id.strip() else None
        verified_thread_id = request._bridge_thread_id or clean_thread_hdr
        raw_explicit_session_id = (
            verified_thread_id
            or model_session_tag
            or clean_x_bridge
            or clean_x_sid
            or clean_req_sid
        )
        is_probe_req = bool(
            (isinstance(x_bridge_probe, str) and x_bridge_probe.strip() == "1")
            or context_builder.is_probe_messages(request.messages)
        )
        explicit_session_id = None if is_probe_req else principal.scope_session_id(raw_explicit_session_id)

        upstream = _select_upstream(model_name)
        target_web_model = resolve_target_web_model(model_name)
        norm_tools = normalize_request_tools(request.tools, request.functions)
        norm_tool_choice = normalize_tool_choice(request.tool_choice, request.function_call)
        tools_active = is_tool_generation_enabled(norm_tools, norm_tool_choice)
        request_task_type = _ledger_task_var.get()
        is_compaction_req = bool(
            request_task_type == "compaction"
            or context_builder.is_compaction_messages(request.messages)
        )
        skip_historical_attachments = upstream == "browser" and (
            is_compaction_req
            or has_coding_tools(norm_tools)
            or has_tool_history(request.messages)
        )
        old_image_markers = (
            historical_image_markers(
                request.messages,
                compaction=is_compaction_req,
                include_all_attachments=True,
            )
            if skip_historical_attachments
            else set()
        )
        try:
            input_attachments = collect_attachments(
                request.messages,
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
                pending_only=True,
                compaction=is_compaction_req,
                tenant_id=principal.tenant_id,
            )
        except InvalidAttachmentError as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", exc.error_code)
        except ValueError as exc:
            return _openai_error_response(400, str(exc), "invalid_request_error", "invalid_attachment")

        completion_id = uuid.uuid4().hex
        created_ts = int(time.time())
        req_started_mono = time.monotonic()
        prompt_preview_text = "[一键模型连通体检]" if is_probe_req else ("[上下文压缩归档]" if is_compaction_req else _extract_messages_preview(request.messages))
        clean_visible_sid = _unscope_session_header(explicit_session_id, principal) or ""
        t_label_for_err = "本机直连" if principal.is_host else f"客户端 · {principal.name}"
        superseded_compactions: list[PendingCompactionCleanup] = (
            []
            if is_compaction_req
            else context_builder.find_superseded_compactions(
                request.messages,
                explicit_session_id=explicit_session_id,
                consumed_tokens=_consumed_compactions_var.get(),
                tenant_id=principal.tenant_id,
            )
        )
        include_usage = bool(request.stream_options and request.stream_options.include_usage)

        json_mode_active = isinstance(request.response_format, dict) and str(
            request.response_format.get("type") or ""
        ).lower() in {"json_object", "json_schema"}
        suppress_citations = tools_active or json_mode_active

        # ---------------- Option 1: Official GCloud Upstream ----------------
        usage_prompt = prompt_for_usage(request)
        input_att_tokens = estimate_attachment_input_tokens(input_attachments)
        logical_in_tokens = estimate_tokens(usage_prompt) + input_att_tokens

        def _resolve_turn_attachments(prepared) -> tuple[list[dict[str, Any]], set[str]]:
            if skip_historical_attachments or prepared.strategy_used not in {"smart_packed_context", "transcript"}:
                return input_attachments, old_image_markers
            return collect_rebuild_attachments(
                request.messages,
                input_attachments,
                file_store=file_store,
                callback_base_url=callback_origin,
                image_output_dir=image_output_dir,
                tenant_id=principal.tenant_id,
            )

        def _select_sent_attachments(prepared, turn_attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not turn_attachments:
                return []
            matched = [att for att in turn_attachments if att["id"] in prepared.prompt]
            if matched:
                return matched
            if not prepared.prompt.strip() and prepared.attachments:
                pending_ids = {att["id"] for att in input_attachments}
                return [att for att in turn_attachments if att["id"] in pending_ids] or list(turn_attachments)
            return []

        logger.debug(
            "chat_completions model=%s upstream=%s stream=%s messages=%d tools=%d attachments=%d session=%s",
            raw_model_name,
            upstream,
            request.stream,
            len(request.messages),
            len(norm_tools),
            len(input_attachments),
            explicit_session_id or "-",
        )
        if LOG_MANAGER.log_payloads:
            logger.debug(
                "chat_completions payload summary: %s",
                summarize_for_debug({"model": raw_model_name, "messages_tail": [m.model_dump(exclude_none=True) for m in request.messages[-2:]]}),
            )

        if upstream == "gcloud":
            if not gcloud.is_configured():
                return _openai_error_response(
                    status_code=503,
                    message=f"未配置 GCP_PROJECT_ID，无法使用 gemini-enterprise-gcloud 模式。请在 .env 中配置或改用 {DEFAULT_MODEL_ID}。",
                    error_type="service_unavailable_error",
                    code="gcloud_not_configured",
                )
            max_out = request.max_completion_tokens or request.max_tokens

            if not request.stream:
                try:
                    async with acquire_request_permit(principal, raw_model_name):
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
                    distribution.record_usage(principal, error=True)
                    usage_ledger.record_failed_request(
                        request_id=completion_id,
                        requested_model=model_name,
                        task_type=request_task_type,
                        tenant_id=principal.tenant_id,
                        tenant_name=t_label_for_err,
                        client_ip=principal.client_ip,
                        session_id=clean_visible_sid,
                        prompt_preview=prompt_preview_text,
                        status="error",
                        status_code=502,
                        error_code="gcloud_failed",
                        error_message=str(exc),
                        slot_key="gcloud",
                        duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                    )
                    return _openai_error_response(502, str(exc), "upstream_error", "gcloud_failed")

                parsed = parse_assistant_output(
                    raw_reply,
                    tools=norm_tools,
                    tool_choice=norm_tool_choice,
                    parallel_tool_calls=request.parallel_tool_calls,
                    response_format=request.response_format,
                )
                resp_gcloud = build_chat_completion_response(
                    request_id=completion_id,
                    model=model_name,
                    reply_text=parsed.content,
                    prompt_text=usage_prompt,
                    created=created_ts,
                    tool_calls=parsed.tool_calls or None,
                    finish_reason=parsed.finish_reason,
                )
                gc_usage = resp_gcloud.get("usage") or {}
                _record_request_with_tenant(
                    principal,
                    request_id=completion_id,
                    requested_model=model_name,
                    prompt_tokens=logical_in_tokens,
                    completion_tokens=int(gc_usage.get("completion_tokens") or 0),
                    gcloud_fallback_model=cfg.gcp_vertex_model,
                    task_type=request_task_type,
                    strategy="gcloud_direct",
                    slot_key="gcloud",
                    session_id=explicit_session_id,
                    prompt_preview=prompt_preview_text,
                    reply_preview=_extract_reply_preview(parsed.content, parsed.tool_calls),
                    tool_calls_count=len(parsed.tool_calls or []),
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                return resp_gcloud

            gc_stream_lease = await _StreamPermitLease(principal, raw_model_name).enter()

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
                    _record_request_with_tenant(
                        principal,
                        request_id=completion_id,
                        requested_model=model_name,
                        prompt_tokens=logical_in_tokens,
                        completion_tokens=estimate_tokens(completion_usage_str),
                        gcloud_fallback_model=cfg.gcp_vertex_model,
                        task_type=request_task_type,
                        strategy="gcloud_stream",
                        slot_key="gcloud",
                        session_id=explicit_session_id,
                        prompt_preview=prompt_preview_text,
                        reply_preview=_extract_reply_preview(parsed.content, parsed.tool_calls),
                        tool_calls_count=len(parsed.tool_calls or []),
                        duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                    )
                except GCloudUpstreamError as exc:
                    distribution.record_usage(principal, error=True)
                    yield "data: " + json.dumps({"error": {"message": str(exc), "type": "upstream_error", "code": "gcloud_failed"}}, ensure_ascii=False) + "\n\n"
                    return
                finally:
                    await gc_stream_lease.close()

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
                        usage=build_usage(usage_prompt, completion_usage_str),
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
            completed_compaction_rec: Optional[PendingCompactionCleanup] = None
            if not is_probe_req:
                distribution.register_live_task(
                    completion_id,
                    principal,
                    model=model_name,
                    task_type=request_task_type,
                    session_id=clean_visible_sid,
                    prompt_preview=prompt_preview_text,
                    stream=False,
                    state="queued",
                )
            try:
                async with (
                    acquire_request_permit(principal, raw_model_name),
                    conversation_slot(
                        request_messages=request.messages,
                        model_name=model_name,
                        explicit_session_id=explicit_session_id,
                        tools_active=tools_active,
                        queue_timeout=cfg.queue_timeout_seconds,
                        ephemeral_probe=is_probe_req,
                        ephemeral_compaction=is_compaction_req,
                        tenant_id=principal.tenant_id,
                        strict_session_affinity=bool(verified_thread_id),
                    ) as (slot, effective_sid, resumed_from_archive),
                ):
                    session_key = slot.slot_key
                    if not is_probe_req:
                        distribution.update_live_task(
                            completion_id,
                            state="running",
                            slot_key=session_key,
                            tab_id=slot.tab_id,
                        )
                    prepared = context_builder.build(
                        messages=request.messages,
                        model=model_name,
                        session_key=session_key,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )
                    turn_attachments, turn_skipped_markers = _resolve_turn_attachments(prepared)
                    prepared.prompt = reference_historical_images(prepared.prompt, turn_skipped_markers)
                    sent_attachments = _select_sent_attachments(prepared, turn_attachments)
                    turn_logical_in_tokens = estimate_tokens(usage_prompt) + estimate_attachment_input_tokens(turn_attachments)
                    record_context(request, prepared, turn_attachments, turn_skipped_markers, sent_count=len(sent_attachments))
                    if resumed_from_archive and principal.is_host:
                        context_diagnostics["resumed_archived_session"] = True
                    raw_out_images: list[dict[str, Any]] = []
                    raw_out_files: list[dict[str, Any]] = []
                    raw_reply = await hub.dispatch_complete_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                        attachments=sent_attachments,
                        output_images=raw_out_images,
                        output_files=raw_out_files,
                    )
                    if superseded_compactions:
                        schedule_compaction_resets(superseded_compactions, exclude_slot_key=session_key)
                    saved_outputs = persist_browser_outputs(
                        raw_out_images,
                        raw_out_files,
                        image_output_dir,
                        file_store,
                        pub_base_url,
                        tenant_id=principal.tenant_id,
                    )
                    parsed = parse_assistant_output(
                        raw_reply, tools=norm_tools, tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )
                    record_reply_protocol(parsed, session_key, request)
                    if saved_outputs and not parsed.has_tool_calls and not json_mode_active:
                        md_links = format_artifacts_markdown(saved_outputs, parsed.content or "")
                        if md_links and (not tools_active or not (parsed.content or "").strip()):
                            parsed.content = ((parsed.content or "").rstrip() + "\n\n" + md_links).strip()
                    slot.pending_tool_call_until = (
                        time.time() + 60.0 if parsed.has_tool_calls else 0.0
                    )
                    effective_slot_model = slot.active_model or target_web_model
                    sent_att_toks = estimate_attachment_input_tokens(sent_attachments)
                    web_sent_toks = estimate_tokens(prepared.prompt) + sent_att_toks
                    reused_toks = (
                        max(0, turn_logical_in_tokens - web_sent_toks)
                        if prepared.strategy_used == "smart_incremental_delta"
                        else 0
                    )
                    comp_for_ledger = (parsed.content or "") + (
                        " " + json.dumps(parsed.tool_calls, ensure_ascii=False)
                        if parsed.has_tool_calls
                        else ""
                    )
                    if is_compaction_req:
                        # Compaction is a one-shot summary pass; clear the worker slot so it never
                        # masquerades as an uncompacted active conversation in the remaining-pool bar.
                        context_builder.record_completed_turn(
                            session_key=session_key,
                            sent_history=prepared.normalized_history,
                            assistant_reply=parsed.content or "",
                            active_model=effective_slot_model,
                            tools_fingerprint=prepared.tools_fingerprint,
                            system_fingerprint=prepared.system_fingerprint,
                            logical_prompt_tokens=turn_logical_in_tokens,
                            web_prompt_tokens=web_sent_toks,
                            last_strategy=prepared.strategy_used,
                            session_url=slot.tab_url or None,
                            explicit_session_id=None,
                            is_compaction=True,
                            tenant_id=principal.tenant_id,
                        )
                        if (parsed.content or "").strip():
                            completed_compaction_rec = context_builder.register_completed_compaction(
                                session_key,
                                session_url=slot.tab_url or None,
                                origin_session_id=explicit_session_id,
                                summary_text=parsed.content or "",
                                pre_compact_turn_count=len(prepared.normalized_history),
                                tenant_id=principal.tenant_id,
                            )
                            if request_task_type == "compaction":
                                _latest_compaction_rec_var.set(completed_compaction_rec)
                        slot_ctx = {
                            "total_context_tokens": turn_logical_in_tokens + estimate_tokens(comp_for_ledger),
                            "context_limit_tokens": CARBON_CONTEXT_WINDOW,
                        }
                    else:
                        context_builder.record_completed_turn(
                            session_key=session_key,
                            sent_history=prepared.normalized_history,
                            assistant_reply=parsed.content or "",
                            assistant_tool_calls=parsed.tool_calls or None,
                            active_model=effective_slot_model,
                            tools_fingerprint=prepared.tools_fingerprint,
                            system_fingerprint=prepared.system_fingerprint,
                            logical_prompt_tokens=turn_logical_in_tokens,
                            web_prompt_tokens=web_sent_toks,
                            last_strategy=prepared.strategy_used,
                            session_url=slot.tab_url or None,
                            explicit_session_id=effective_sid,
                            tenant_id=principal.tenant_id,
                        )
                        slot_ctx = context_builder.get_slot_context_metrics(session_key) or {}
                    _record_request_with_tenant(
                        principal,
                        request_id=completion_id,
                        requested_model=model_name,
                        prompt_tokens=turn_logical_in_tokens,
                        completion_tokens=estimate_tokens(comp_for_ledger),
                        browser_active_model=effective_slot_model,
                        task_type=request_task_type,
                        strategy=prepared.strategy_used,
                        cached_tokens=reused_toks,
                        delta_prompt_tokens=max(0, turn_logical_in_tokens - reused_toks),
                        images_generated=sum(1 for a in (saved_outputs or []) if a.get("category") == "image"),
                        videos_generated=sum(1 for a in (saved_outputs or []) if a.get("category") == "video"),
                        slot_key=session_key,
                        tab_id=slot.tab_id,
                        session_id=effective_sid or explicit_session_id,
                        session_title=context_builder.get_session_title(session_key),
                        prompt_preview=prompt_preview_text,
                        reply_preview=_extract_reply_preview(parsed.content, parsed.tool_calls),
                        tool_calls_count=len(parsed.tool_calls or []),
                        context_tokens=slot_ctx.get("total_context_tokens"),
                        context_limit=slot_ctx.get("context_limit_tokens"),
                        duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                    )
                    slot_header_val = slot.slot_key
            except BridgeUnavailableError as exc:
                distribution.record_usage(principal, error=True)
                usage_ledger.record_failed_request(
                    request_id=completion_id, requested_model=model_name, task_type=request_task_type,
                    tenant_id=principal.tenant_id, tenant_name=t_label_for_err, client_ip=principal.client_ip,
                    session_id=clean_visible_sid, prompt_preview=prompt_preview_text,
                    status="error", status_code=503, error_code="browser_not_connected", error_message=str(exc),
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                return _openai_error_response(503, str(exc), "bridge_unavailable_error", "browser_not_connected")
            except BridgeTimeoutError as exc:
                distribution.record_usage(principal, error=True)
                usage_ledger.record_failed_request(
                    request_id=completion_id, requested_model=model_name, task_type=request_task_type,
                    tenant_id=principal.tenant_id, tenant_name=t_label_for_err, client_ip=principal.client_ip,
                    session_id=clean_visible_sid, prompt_preview=prompt_preview_text,
                    status="timeout", status_code=504, error_code="browser_timeout", error_message=str(exc),
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                return _openai_error_response(504, str(exc), "timeout_error", "browser_timeout")
            except (BridgeExecutionError, ToolProtocolError, BrowserTaskFailure) as exc:
                distribution.record_usage(principal, error=True)
                usage_ledger.record_failed_request(
                    request_id=completion_id, requested_model=model_name, task_type=request_task_type,
                    tenant_id=principal.tenant_id, tenant_name=t_label_for_err, client_ip=principal.client_ip,
                    session_id=clean_visible_sid, prompt_preview=prompt_preview_text,
                    status="error", status_code=502, error_code="dom_execution_failed", error_message=str(exc),
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                return _openai_error_response(502, str(exc), "bridge_execution_error", "dom_execution_failed")
            finally:
                if not is_probe_req:
                    distribution.finish_live_task(completion_id)

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
            resp_headers = {"X-Bridge-Slot": slot_header_val}
            client_visible_sid = _unscope_session_header(explicit_session_id, principal)
            if client_visible_sid:
                resp_headers["X-Bridge-Session-Id"] = client_visible_sid
            if completed_compaction_rec is not None and request_task_type != "compaction":
                context_builder.mark_compaction_delivered(
                    completed_compaction_rec.slot_key,
                    completed_compaction_rec.compaction_seq,
                )
            return JSONResponse(content=resp_payload, headers=resp_headers)

        browser_stream_lease = await _StreamPermitLease(principal, raw_model_name).enter()
        if not is_probe_req:
            distribution.register_live_task(
                completion_id,
                principal,
                model=model_name,
                task_type=request_task_type,
                session_id=clean_visible_sid,
                prompt_preview=prompt_preview_text,
                stream=True,
                state="queued",
            )

        async def close_browser_stream():
            if not is_probe_req:
                distribution.finish_live_task(completion_id)
            await browser_stream_lease.close()

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
                handoff_cleanup_triggered = False
                stream_compaction_rec: Optional[PendingCompactionCleanup] = None
                async with (
                    conversation_slot(
                        request_messages=request.messages,
                        model_name=model_name,
                        explicit_session_id=explicit_session_id,
                        tools_active=tools_active,
                        queue_timeout=cfg.queue_timeout_seconds,
                        ephemeral_probe=is_probe_req,
                        ephemeral_compaction=is_compaction_req,
                        tenant_id=principal.tenant_id,
                        strict_session_affinity=bool(verified_thread_id),
                    ) as (slot, effective_sid, resumed_from_archive),
                    AsyncExitStack() as stream_cleanup,
                ):
                    session_key = slot.slot_key
                    if not is_probe_req:
                        distribution.update_live_task(
                            completion_id,
                            state="running",
                            slot_key=session_key,
                            tab_id=slot.tab_id,
                        )
                    prepared = context_builder.build(
                        messages=request.messages,
                        model=model_name,
                        session_key=session_key,
                        tools=norm_tools,
                        tool_choice=norm_tool_choice,
                        parallel_tool_calls=request.parallel_tool_calls,
                        response_format=request.response_format,
                    )

                    turn_attachments, turn_skipped_markers = _resolve_turn_attachments(prepared)
                    prepared.prompt = reference_historical_images(prepared.prompt, turn_skipped_markers)
                    sent_attachments = _select_sent_attachments(prepared, turn_attachments)
                    turn_logical_in_tokens = estimate_tokens(usage_prompt) + estimate_attachment_input_tokens(turn_attachments)
                    record_context(request, prepared, turn_attachments, turn_skipped_markers, sent_count=len(sent_attachments))
                    if resumed_from_archive and principal.is_host:
                        context_diagnostics["resumed_archived_session"] = True
                    stream_out_images: list[dict[str, Any]] = []
                    stream_out_files: list[dict[str, Any]] = []
                    dispatch_stream = hub.dispatch_stream_on_slot(
                        slot=slot,
                        prompt=prepared.prompt,
                        target_model=target_web_model,
                        reset_chat=prepared.reset_chat,
                        suppress_citations=suppress_citations,
                        request_timeout=cfg.request_timeout_seconds,
                        attachments=sent_attachments,
                        output_images=stream_out_images,
                        output_files=stream_out_files,
                    )
                    stream_cleanup.push_async_callback(dispatch_stream.aclose)
                    async for delta in dispatch_stream:
                        if (
                            not handoff_cleanup_triggered
                            and superseded_compactions
                            and (slot.last_dispatch_submitted or bool(delta))
                        ):
                            handoff_cleanup_triggered = True
                            schedule_compaction_resets(
                                superseded_compactions, exclude_slot_key=session_key
                            )
                        if not delta:
                            if time.monotonic() - last_sse_ts >= 12.0:
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

                    tail_pieces, parsed = processor.finalize()
                    if not handoff_cleanup_triggered and superseded_compactions:
                        handoff_cleanup_triggered = True
                        schedule_compaction_resets(
                            superseded_compactions, exclude_slot_key=session_key
                        )
                    record_reply_protocol(parsed, session_key, request)
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
                        stream_out_images,
                        stream_out_files,
                        image_output_dir,
                        file_store,
                        pub_base_url,
                        tenant_id=principal.tenant_id,
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
                    sent_att_toks = estimate_attachment_input_tokens(sent_attachments)
                    web_sent_toks = estimate_tokens(prepared.prompt) + sent_att_toks
                    reused_toks = (
                        max(0, turn_logical_in_tokens - web_sent_toks)
                        if prepared.strategy_used == "smart_incremental_delta"
                        else 0
                    )
                    turn_completed = False
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
                            logical_prompt_tokens=turn_logical_in_tokens,
                            web_prompt_tokens=web_sent_toks,
                            last_strategy=prepared.strategy_used,
                            session_url=slot.tab_url or None,
                            explicit_session_id=effective_sid,
                            is_compaction=is_compaction_req,
                            tenant_id=principal.tenant_id,
                        )
                        turn_completed = True
                    else:
                        full_reply = (parsed.content or "").strip()
                        completion_usage_str = full_reply
                        if full_reply or saved_stream_outputs:
                            context_builder.record_completed_turn(
                                session_key=session_key,
                                sent_history=prepared.normalized_history,
                                assistant_reply=full_reply,
                                active_model=effective_slot_model,
                                tools_fingerprint=prepared.tools_fingerprint,
                                system_fingerprint=prepared.system_fingerprint,
                                logical_prompt_tokens=turn_logical_in_tokens,
                                web_prompt_tokens=web_sent_toks,
                                last_strategy=prepared.strategy_used,
                                session_url=slot.tab_url or None,
                                explicit_session_id=effective_sid,
                                is_compaction=is_compaction_req,
                                tenant_id=principal.tenant_id,
                            )
                            if is_compaction_req and full_reply:
                                stream_compaction_rec = context_builder.register_completed_compaction(
                                    session_key,
                                    session_url=slot.tab_url or None,
                                    origin_session_id=explicit_session_id,
                                    summary_text=full_reply,
                                    pre_compact_turn_count=len(prepared.normalized_history),
                                    tenant_id=principal.tenant_id,
                                )
                            turn_completed = True
                    if turn_completed:
                        slot_ctx = context_builder.get_slot_context_metrics(session_key) or {}
                        _record_request_with_tenant(
                            principal,
                            request_id=completion_id,
                            requested_model=model_name,
                            prompt_tokens=turn_logical_in_tokens,
                            completion_tokens=estimate_tokens(completion_usage_str),
                            browser_active_model=effective_slot_model,
                            task_type=request_task_type,
                            strategy=prepared.strategy_used,
                            cached_tokens=reused_toks,
                            delta_prompt_tokens=max(0, turn_logical_in_tokens - reused_toks),
                            images_generated=sum(1 for a in (saved_stream_outputs or []) if a.get("category") == "image"),
                            videos_generated=sum(1 for a in (saved_stream_outputs or []) if a.get("category") == "video"),
                            slot_key=session_key,
                            tab_id=slot.tab_id,
                            session_id=effective_sid or explicit_session_id,
                            session_title=context_builder.get_session_title(session_key),
                            prompt_preview=prompt_preview_text,
                            reply_preview=_extract_reply_preview(parsed.content, parsed.tool_calls),
                            tool_calls_count=len(parsed.tool_calls or []),
                            context_tokens=slot_ctx.get("total_context_tokens"),
                            context_limit=slot_ctx.get("context_limit_tokens"),
                            duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                        )
            except (BridgeUnavailableError, BridgeTimeoutError, BridgeExecutionError, ToolProtocolError, BrowserTaskFailure) as exc:
                distribution.record_usage(principal, error=True)
                usage_ledger.record_failed_request(
                    request_id=completion_id, requested_model=model_name, task_type=request_task_type,
                    tenant_id=principal.tenant_id, tenant_name=t_label_for_err, client_ip=principal.client_ip,
                    session_id=clean_visible_sid, prompt_preview=prompt_preview_text,
                    status="timeout" if isinstance(exc, BridgeTimeoutError) else "error",
                    status_code=504 if isinstance(exc, BridgeTimeoutError) else 502,
                    error_code="browser_stream_failed", error_message=str(exc),
                    duration_ms=int((time.monotonic() - req_started_mono) * 1000),
                )
                yield "data: " + json.dumps({"error": {"message": str(exc), "type": "bridge_error", "code": "browser_failed"}}, ensure_ascii=False) + "\n\n"
                return
            finally:
                await close_browser_stream()

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

            if stream_compaction_rec is not None:
                context_builder.mark_compaction_delivered(
                    stream_compaction_rec.slot_key,
                    stream_compaction_rec.compaction_seq,
                )
            yield "data: [DONE]\n\n"

        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        client_visible_sid = _unscope_session_header(explicit_session_id, principal)
        if client_visible_sid:
            sse_headers["X-Bridge-Session-Id"] = client_visible_sid

        return ClosingStreamingResponse(
            with_keepalive(browser_sse()),
            media_type="text/event-stream",
            headers=sse_headers,
            on_close=close_browser_stream,
        )

    return app


_default_app: Optional[FastAPI] = None


def __getattr__(name: str) -> Any:
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    import uvicorn

    runtime_cfg = BridgeSettings.from_env()
    LOG_MANAGER.configure(
        level=runtime_cfg.log_level,
        debug=runtime_cfg.debug,
        log_file=runtime_cfg.log_file,
        log_payloads=runtime_cfg.log_payloads,
    )
    uvicorn.run(
        create_app(runtime_cfg),
        host=runtime_cfg.host,
        port=runtime_cfg.port,
        log_level="debug" if LOG_MANAGER.is_debug_enabled() else "info",
        ws_max_size=16 * 1024 * 1024,
        h11_max_incomplete_event_size=16 * 1024 * 1024,
        timeout_graceful_shutdown=5,
    )

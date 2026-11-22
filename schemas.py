"""OpenAI-compatible request/response schemas, model alias resolver, and token helpers."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, PrivateAttr

from multimodal import NormalizedAttachment, extract_attachments_from_content

try:
    from pydantic import ConfigDict

    _PYDANTIC_V2 = True
except ImportError:  # pragma: no cover
    _PYDANTIC_V2 = False


class _FlexibleModel(BaseModel):
    """Base model ignoring unknown client fields (Cursor, Cline, NextChat, ChatBox, SDKs)."""

    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="ignore")
    else:  # pragma: no cover

        class Config:
            extra = "ignore"


class StreamOptions(_FlexibleModel):
    include_usage: bool = False


class ChatMessage(_FlexibleModel):
    role: str
    content: Optional[Union[str, List[Any]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    function_call: Optional[Dict[str, Any]] = None
    tool_call_id: Optional[str] = None
    images: Optional[List[Any]] = None
    files: Optional[List[Any]] = None
    attachments: Optional[List[Any]] = None
    audio: Optional[Dict[str, Any]] = None

    def text_content(self) -> str:
        base = extract_text_content(self.content)
        if not (self.images or self.files or self.attachments or self.audio):
            return base
        from media_inputs import extract_part_marker, iter_message_multimodal_parts
        extra_markers: List[str] = []
        for part in iter_message_multimodal_parts(self):
            marker = extract_part_marker(part)
            if marker and marker not in base and marker not in extra_markers:
                extra_markers.append(marker)
        if not extra_markers:
            return base
        return "\n".join([p for p in [base, *extra_markers] if p])

    def extract_attachments(self) -> List[NormalizedAttachment]:
        extra_files: List[Any] = []
        if isinstance(self.files, list):
            extra_files.extend(self.files)
        if isinstance(self.attachments, list):
            extra_files.extend(self.attachments)
        return extract_attachments_from_content(
            self.content,
            extra_images=self.images,
            extra_files=extra_files or None,
            extra_audio=self.audio if isinstance(self.audio, dict) else None,
        )

    def normalized_tool_calls(self) -> List[Dict[str, Any]]:
        """Return normalized OpenAI tool_calls from `tool_calls`, legacy `function_call`, or `tool_use` blocks."""
        calls: List[Dict[str, Any]] = []

        if isinstance(self.tool_calls, list):
            for idx, item in enumerate(self.tool_calls):
                if not isinstance(item, dict):
                    continue
                fn = item.get("function") if isinstance(item.get("function"), dict) else item
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                raw_args = fn.get("arguments", "{}")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                tc_id = str(item.get("id") or f"call_hist_{idx}")
                calls.append(
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": raw_args},
                    }
                )

        if not calls and isinstance(self.function_call, dict):
            name = str(self.function_call.get("name") or "").strip()
            if name:
                raw_args = self.function_call.get("arguments", "{}")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                calls.append(
                    {
                        "id": f"call_fn_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {"name": name, "arguments": raw_args},
                    }
                )

        if isinstance(self.content, list):
            for idx, block in enumerate(self.content):
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "").lower()
                if btype == "tool_use" and block.get("name"):
                    raw_in = block.get("input", {})
                    args_str = (
                        raw_in
                        if isinstance(raw_in, str)
                        else json.dumps(raw_in, ensure_ascii=False)
                    )
                    calls.append(
                        {
                            "id": str(block.get("id") or f"call_tu_{idx}"),
                            "type": "function",
                            "function": {
                                "name": str(block["name"]).strip(),
                                "arguments": args_str,
                            },
                        }
                    )

        return calls


class ChatCompletionRequest(_FlexibleModel):
    _bridge_thread_id: Optional[str] = PrivateAttr(default=None)
    model: str = Field(default="")
    messages: List[ChatMessage]
    stream: bool = False
    stream_options: Optional[StreamOptions] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = True
    functions: Optional[List[Dict[str, Any]]] = None
    function_call: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    user: Optional[str] = None
    session_id: Optional[str] = None
    prompt_cache_key: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class BridgeRegisterRequest(_FlexibleModel):
    client_id: Optional[str] = None
    user_agent: Optional[str] = None
    tab_id: Optional[int] = None
    tab_url: Optional[str] = None
    tab_title: Optional[str] = None
    driver_mode: str = "auto"
    active_model: Optional[str] = None
    available_models: Optional[List[str]] = None
    tabs: Optional[List[Dict[str, Any]]] = None
    capabilities: Optional[List[str]] = None


class BridgeEventPayload(_FlexibleModel):
    client_id: str
    event: str  # heartbeat | request_ack | stream_chunk | request_complete | request_error | tab_status
    request_id: Optional[str] = None
    tab_id: Optional[int] = None
    delta: Optional[str] = None
    reply: Optional[str] = None
    error: Optional[str] = None
    output_images: Optional[List[Dict[str, Any]]] = None
    output_files: Optional[List[Dict[str, Any]]] = None
    citations: Optional[List[Dict[str, Any]]] = None
    source: Optional[str] = None
    tab_url: Optional[str] = None
    tab_title: Optional[str] = None
    ready: Optional[bool] = None
    driver_mode: Optional[str] = None
    captured_rpc: Optional[bool] = None
    active_model: Optional[str] = None
    available_models: Optional[List[str]] = None
    tabs: Optional[List[Dict[str, Any]]] = None
    capabilities: Optional[List[str]] = None
    chunk_index: Optional[int] = None
    total_chunks: Optional[int] = None
    data: Optional[str] = None


class BridgeCallbackPayload(_FlexibleModel):
    """Direct HTTP callback upload for large (>32KB / 100KB+) replies & tool calls."""

    client_id: Optional[str] = None
    tab_id: Optional[int] = None
    tab_url: Optional[str] = None
    tab_title: Optional[str] = None
    request_id: str
    reply: Optional[str] = None
    error: Optional[str] = None
    submitted: Optional[bool] = None
    tab_reset: Optional[bool] = None
    output_images: Optional[List[Dict[str, Any]]] = None
    output_files: Optional[List[Dict[str, Any]]] = None
    citations: Optional[List[Dict[str, Any]]] = None
    source: Optional[str] = "http_callback"
    active_model: Optional[str] = None
    available_models: Optional[List[str]] = None


def extract_text_content(content: Optional[Union[str, List[Any]]]) -> str:
    """Normalize OpenAI string or multimodal/tool content-part arrays into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            part_type = str(item.get("type", "text")).lower()
            if part_type in {"text", "input_text", "output_text"}:
                text_val = item.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
                elif isinstance(text_val, dict) and isinstance(text_val.get("value"), str):
                    parts.append(text_val["value"])
            elif part_type == "refusal" and isinstance(item.get("refusal"), str):
                parts.append(item["refusal"])
            elif part_type == "tool_result":
                call_id = str(item.get("tool_use_id") or item.get("tool_call_id") or "")
                inner = extract_text_content(item.get("content"))
                if not inner and extract_attachments_from_content(item.get("content")):
                    inner = "[Attached file(s) in tool result]"
                header = f'[TOOL_RESULT call_id="{call_id}"]' if call_id else "[TOOL_RESULT]"
                parts.append(f"{header}\n{inner}\n[/TOOL_RESULT]")
            elif part_type in {
                "image_url", "input_image", "image",
                "file", "input_file", "document",
                "input_audio", "audio_url", "audio",
                "video_url", "input_video", "video",
            }:
                from media_inputs import extract_part_marker
                marker = extract_part_marker(item)
                if marker:
                    parts.append(marker)
        return "\n".join(p for p in parts if p)
    return str(content)


_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


def estimate_tokens(text: str) -> int:
    """Gemini CLI-style conservative estimate; not Google usage or billing."""
    if not text:
        return 0
    non_ascii = len(_NON_ASCII_RE.findall(text))
    return max(1, int(non_ascii * 1.5 + (len(text) - non_ascii) / 4))


def build_usage(prompt_text: str, completion_text: str) -> Dict[str, int]:
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(completion_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def build_chat_completion_response(
    request_id: str,
    model: str,
    reply_text: Optional[str],
    prompt_text: str = "",
    created: Optional[int] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    finish_reason: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    ts = created if created is not None else int(time.time())
    has_tools = bool(tool_calls)
    resolved_finish = finish_reason or ("tool_calls" if has_tools else "stop")

    message_obj: Dict[str, Any] = {
        "role": "assistant",
        "content": reply_text if (reply_text is not None or not has_tools) else None,
    }
    if has_tools:
        message_obj["tool_calls"] = tool_calls
    if attachments:
        message_obj["attachments"] = attachments

    completion_for_usage = reply_text or ""
    if has_tools:
        completion_for_usage += " " + json.dumps(tool_calls, ensure_ascii=False)

    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": ts,
        "model": model,
        "system_fingerprint": "fp_gemini_enterprise_bridge",
        "choices": [
            {
                "index": 0,
                "message": message_obj,
                "logprobs": None,
                "finish_reason": resolved_finish,
            }
        ],
        "usage": build_usage(prompt_text, completion_for_usage),
    }


def build_chat_completion_chunk(
    request_id: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    created: Optional[int] = None,
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    ts = created if created is not None else int(time.time())
    payload: Dict[str, Any] = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": ts,
        "model": model,
        "system_fingerprint": "fp_gemini_enterprise_bridge",
        "choices": (
            []
            if usage is not None and not delta and finish_reason is None
            else [
                {
                    "index": 0,
                    "delta": delta,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ]
        ),
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


# ---------------------------------------------------------------------------
# Web Model Selection Registry & Shorthand Resolver
# ---------------------------------------------------------------------------

WEB_MODEL_ALIASES: Dict[str, str] = {
    # Gemini Auto (explicitly switch web UI selector back to Auto)
    "gemini-auto": "auto",
    "web-auto": "auto",
    # Gemini 3.1 Pro
    "gemini-3.1-pro": "3.1-pro",
    "gemini3.1pro": "3.1-pro",
    "gemini-3.1pro": "3.1-pro",
    "3.1-pro": "3.1-pro",
    "3.1pro": "3.1-pro",
    "3.1": "3.1-pro",
    # Gemini 4 Argon
    "gemini-4-argon": "4-argon",
    "gemini-4.0-argon": "4-argon",
    "gemini4.0argon": "4-argon",
    "gemini4argon": "4-argon",
    "gemini-4argon": "4-argon",
    "4-argon": "4-argon",
    "4.0-argon": "4-argon",
    "4argon": "4-argon",
    "argon": "4-argon",
    # Gemini 4 Carbon
    "gemini-4-carbon": "4-carbon",
    "gemini-4.0-carbon": "4-carbon",
    "gemini4.0carbon": "4-carbon",
    "gemini4carbon": "4-carbon",
    "gemini-4carbon": "4-carbon",
    "4-carbon": "4-carbon",
    "4.0-carbon": "4-carbon",
    "4carbon": "4-carbon",
    "carbon": "4-carbon",
    # Gemini 4 Hydrogen
    "gemini-4-hydrogen": "4-hydrogen",
    "gemini-4.0-hydrogen": "4-hydrogen",
    "gemini4.0hydrogen": "4-hydrogen",
    "gemini4hydrogen": "4-hydrogen",
    "gemini-4hydrogen": "4-hydrogen",
    "4-hydrogen": "4-hydrogen",
    "4.0-hydrogen": "4-hydrogen",
    "4hydrogen": "4-hydrogen",
    "hydrogen": "4-hydrogen",
    # Gemini 3.8 Flash
    "gemini-3.8-flash": "3.8-flash",
    "gemini3.8flash": "3.8-flash",
    "gemini-3.8flash": "3.8-flash",
    "3.8-flash": "3.8-flash",
    "3.8flash": "3.8-flash",
    "3.8": "3.8-flash",
    # Gemini 2.5 Pro
    "gemini-2.5-pro": "2.5-pro",
    "gemini2.5pro": "2.5-pro",
    "gemini-2.5pro": "2.5-pro",
    "2.5-pro": "2.5-pro",
    "2.5pro": "2.5-pro",
    # Gemini 2.5 Flash
    "gemini-2.5-flash": "2.5-flash",
    "gemini2.5flash": "2.5-flash",
    "gemini-2.5flash": "2.5-flash",
    "2.5-flash": "2.5-flash",
    "2.5flash": "2.5-flash",
}

DEFAULT_MODEL_ID = "gemini-4-carbon"

_LEGACY_DEFAULT_MODEL_IDS = {
    "",
    "default",
    "auto",
    "web",
    "gemini-enterprise",
    "gemini-enterprise-web",
}
_KEEP_CURRENT_WEB_MODEL_IDS = _LEGACY_DEFAULT_MODEL_IDS

_GCLOUD_PASSTHROUGH_MODEL_IDS = {
    "gemini-enterprise-gcloud",
    "gcloud",
}

_GENERIC_THIRD_PARTY_PREFIXES = (
    "gpt-",
    "o1-",
    "o3-",
    "o4-",
    "claude-",
    "grok-",
    "kimi-",
    "deepseek-",
    "qwen-",
)

_ELEMENT_CODENAMES = {
    "hydrogen",
    "helium",
    "lithium",
    "beryllium",
    "boron",
    "carbon",
    "nitrogen",
    "oxygen",
    "fluorine",
    "neon",
    "sodium",
    "magnesium",
    "aluminum",
    "aluminium",
    "silicon",
    "phosphorus",
    "sulfur",
    "sulphur",
    "chlorine",
    "argon",
    "potassium",
    "calcium",
    "scandium",
    "titanium",
    "vanadium",
    "chromium",
    "manganese",
    "iron",
    "cobalt",
    "nickel",
    "copper",
    "zinc",
    "gallium",
    "germanium",
    "arsenic",
    "selenium",
    "bromine",
    "krypton",
    "rubidium",
    "strontium",
    "yttrium",
    "zirconium",
    "niobium",
    "molybdenum",
    "silver",
    "cadmium",
    "indium",
    "tin",
    "antimony",
    "tellurium",
    "iodine",
    "xenon",
    "cesium",
    "barium",
    "platinum",
    "gold",
    "mercury",
    "thallium",
    "lead",
    "bismuth",
    "radon",
}

_KNOWN_GEMINI_TIERS = {
    "pro",
    "flash",
    "ultra",
    "nano",
    "lite",
    "mini",
    "max",
    "plus",
    "thinking",
    "deep-think",
    "deepthink",
    "experimental",
    "exp",
    "preview",
    "live",
}

_UNIT_EXCLUSIONS = {
    "px",
    "pt",
    "em",
    "rem",
    "vh",
    "vw",
    "ms",
    "sec",
    "secs",
    "min",
    "mins",
    "hr",
    "hrs",
    "kb",
    "mb",
    "gb",
    "tb",
}

_COMPACT_TIER_EXPANSIONS = {
    "flashlite": "flash-lite",
    "flashthinking": "flash-thinking",
    "prothinking": "pro-thinking",
    "deepthink": "deep-think",
}

_VERSIONED_GEMINI_RE = re.compile(
    r"^(?:gemini[\s\-_]*)?(\d+(?:\.\d+)?)[\s\-_]*([a-z][a-z0-9\-_\s]*)$",
    re.IGNORECASE,
)

_GENERIC_WEB_PLACEHOLDER_RE = re.compile(
    r"^(?:auto|自动|choose\s+a?\s*model|select\s+a?\s*model|switch\s+model|model\s+selector|model\s+picker|model|选择模型|切换模型)$",
    re.IGNORECASE,
)


def _parse_dynamic_gemini_target(raw_name: str) -> Optional[str]:
    """Parse future Gemini model strings (`gemini-5-pro`, `5pro`, `4-oxygen`, `gemini4.5flash`, `oxygen`)."""
    cleaned = (raw_name or "").strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    has_gemini_prefix = low.startswith("gemini")

    m = _VERSIONED_GEMINI_RE.match(cleaned)
    if m:
        ver, raw_variant = m.group(1), m.group(2)
        slug = re.sub(r"[\s_\-]+", "-", raw_variant.strip().lower()).strip("-")
        slug = _COMPACT_TIER_EXPANSIONS.get(slug, slug)
        if not slug:
            return None
        if not has_gemini_prefix:
            if slug in _UNIT_EXCLUSIONS:
                return None
            tokens = slug.split("-")
            if not (
                slug in _ELEMENT_CODENAMES
                or any(t in _KNOWN_GEMINI_TIERS or t in _ELEMENT_CODENAMES for t in tokens)
                or len(slug) >= 3
            ):
                return None
        if ver.endswith(".0") and slug in _ELEMENT_CODENAMES:
            ver = ver[:-2]
        return f"{ver}-{slug}"

    if has_gemini_prefix:
        rest = re.sub(r"^gemini[\s\-_]*", "", low).strip("-")
        if rest:
            rest_slug = re.sub(r"[\s_\-]+", "-", rest)
            return _COMPACT_TIER_EXPANSIONS.get(rest_slug, rest_slug)

    if low in _ELEMENT_CODENAMES:
        return low

    return None


def _strip_provider_prefix_if_gemini(raw: str) -> str:
    """Strip `<provider_id>/` prefix (e.g. Copilot BYOK UUIDs) when RHS is a recognized Gemini model."""
    if "/" not in raw or raw.lower().startswith(("gemini-web:", "web:")):
        return raw
    rhs = raw.rsplit("/", 1)[-1].strip()
    if not rhs:
        return raw
    rhs_low = rhs.lower()
    rhs_hyphen = re.sub(r"[\s_]+", "-", rhs_low)
    rhs_compact = re.sub(r"[\s_\-]+", "", rhs_low)
    if (
        rhs_low in _KEEP_CURRENT_WEB_MODEL_IDS
        or rhs_hyphen in WEB_MODEL_ALIASES
        or rhs_compact in WEB_MODEL_ALIASES
        or rhs_low.startswith(("gemini-", "gemini-web:", "web:"))
        or _parse_dynamic_gemini_target(rhs) is not None
    ):
        return rhs
    return raw


def parse_model_and_session(model_name: Optional[str]) -> tuple[str, Optional[str]]:
    """Split optional `@session_tag` or `#session_tag` from model string.

    Examples:
        - `"gemini-4-argon@agent-1"` -> `("gemini-4-argon", "agent-1")`
        - `"3.1pro#tab2"`            -> `("3.1pro", "tab2")`
        - `"gemini-4-argon"`         -> `("gemini-4-argon", None)`
    """
    if not model_name:
        return ("", None)
    raw = model_name.strip()
    for sep in ("@", "#"):
        if sep in raw:
            base, _, tag = raw.partition(sep)
            clean_tag = tag.strip()
            # Support `4argon@sess1-newchat` by moving `-newchat` back onto base model
            if clean_tag.lower().endswith("-newchat") and not base.lower().endswith("-newchat"):
                clean_tag = clean_tag[: -len("-newchat")].strip()
                base = f"{base.strip()}-newchat"
            return (base.strip(), clean_tag or None)
    return (raw, None)


def model_requests_new_chat(model_name: Optional[str]) -> bool:
    base_model, _ = parse_model_and_session(model_name)
    if not base_model:
        return False
    return base_model.strip().lower().endswith("-newchat")


def normalize_default_model(model_name: Optional[str], fallback: str = DEFAULT_MODEL_ID) -> str:
    """Normalize empty or legacy placeholder model IDs (e.g. gemini-enterprise-web) to Carbon."""
    fb_clean = (fallback or DEFAULT_MODEL_ID).strip()
    if fb_clean.lower() in _LEGACY_DEFAULT_MODEL_IDS:
        fb_clean = DEFAULT_MODEL_ID

    if not model_name or not str(model_name).strip():
        return fb_clean

    base_model, session_tag = parse_model_and_session(str(model_name))
    raw = base_model.strip()
    wants_newchat = raw.lower().endswith("-newchat")
    core = raw[: -len("-newchat")].strip() if wants_newchat else raw

    if core.lower() in _LEGACY_DEFAULT_MODEL_IDS:
        rebuilt = f"{fb_clean}-newchat" if wants_newchat and not fb_clean.lower().endswith("-newchat") else fb_clean
        return f"{rebuilt}@{session_tag}" if session_tag else rebuilt

    return str(model_name).strip()


def resolve_target_web_model(model_name: Optional[str]) -> Optional[str]:
    """Resolve an OpenAI `model` parameter into a Gemini Enterprise web UI selector target.

    Returns:
        - `"4-carbon"` by default when model is omitted or uses a legacy placeholder (`gemini-enterprise-web`)
        - `None` when using official GCloud upstream (`gemini-enterprise-gcloud`)
        - Canonical web model key (`"3.1-pro"`, `"4-argon"`, `"4-carbon"`, `"4-hydrogen"`, `"3.8-flash"`, `"2.5-pro"`, `"2.5-flash"`)
        - Or a clean custom UI label for pass-through models (`gemini-web:<label>` or future Gemini versions)
    """
    base_model, _ = parse_model_and_session(model_name)
    if not base_model:
        return "4-carbon"

    raw = base_model.strip()
    if raw.lower().endswith("-newchat"):
        raw = raw[: -len("-newchat")].strip()

    raw = _strip_provider_prefix_if_gemini(raw)
    if not raw:
        return "4-carbon"

    lowered = raw.lower()
    if lowered in _GCLOUD_PASSTHROUGH_MODEL_IDS:
        return None
    if lowered in _LEGACY_DEFAULT_MODEL_IDS:
        return "4-carbon"

    # Explicit custom web label prefix: `gemini-web:<Custom UI Label>` or `web:<Custom UI Label>`
    for prefix in ("gemini-web:", "web:"):
        if lowered.startswith(prefix):
            custom = raw[len(prefix) :].strip()
            if not custom:
                return None
            compact_custom = re.sub(r"[\s_]+", "-", custom.lower())
            return WEB_MODEL_ALIASES.get(compact_custom, custom)

    # Check canonical aliases with flexible hyphen/space/underscore normalization
    normalized_hyphen = re.sub(r"[\s_]+", "-", lowered)
    normalized_compact = re.sub(r"[\s_\-]+", "", lowered)

    if normalized_hyphen in WEB_MODEL_ALIASES:
        return WEB_MODEL_ALIASES[normalized_hyphen]
    if normalized_compact in WEB_MODEL_ALIASES:
        return WEB_MODEL_ALIASES[normalized_compact]

    # Do not try to click dropdown for unconfigured generic OpenAI/Anthropic default model strings
    if lowered in {"gpt-4", "gpt-4o", "o1", "o3"} or lowered.startswith(_GENERIC_THIRD_PARTY_PREFIXES):
        return None

    # Future Gemini models (`gemini-5-pro`, `5pro`, `4-oxygen`, `gemini4.5flash`, `oxygen`, etc.)
    dynamic_target = _parse_dynamic_gemini_target(raw)
    if dynamic_target is not None:
        return dynamic_target

    # Future/custom Gemini Enterprise models: strip optional `gemini-` prefix for cleaner UI matching
    if lowered.startswith("gemini-") and len(raw) > 7:
        return raw[7:].strip()

    return raw


DEFAULT_CONTEXT_WINDOW_TOKENS = 1048576
DEFAULT_MAX_OUTPUT_TOKENS = 65536


def _build_model_entry(
    model_id: str,
    description: str,
    owned_by: str = "google-enterprise-bridge",
    created: int = 1735689600,
) -> Dict[str, Any]:
    """Construct an OpenAI/Copilot/LiteLLM/Cursor-compatible model metadata object."""
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
        "description": description,
        "context_length": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "context_window": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "max_prompt_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "max_input_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "input_token_limit": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "max_completion_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "output_token_limit": DEFAULT_MAX_OUTPUT_TOKENS,
        "max_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "supports_vision": True,
        "input_modalities": ["text", "image", "audio", "video", "file"],
        "output_modalities": ["text", "image", "video"],
        "capabilities": {
            "chat": True,
            "tools": True,
            "function_calling": True,
            "streaming": True,
            "json_mode": True,
            "vision": True,
            "multimodal": True,
        },
        "limits": {
            "max_prompt_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "max_context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        },
    }


AVAILABLE_MODELS: List[Dict[str, Any]] = [
    _build_model_entry(
        "gemini-4-carbon",
        "Primary flagship model — auto-select Gemini 4 Carbon in Gemini Enterprise (aliases: 4carbon, 4-carbon)",
    ),
    _build_model_entry(
        "gemini-4-hydrogen",
        "Auto-select Gemini 4 Hydrogen in Gemini Enterprise web UI (aliases: 4hydrogen, 4-hydrogen)",
    ),
    _build_model_entry(
        "gemini-4-argon",
        "Auto-select Gemini 4 Argon in Gemini Enterprise web UI (aliases: 4argon, 4-argon)",
    ),
    _build_model_entry(
        "gemini-3.1-pro",
        "Auto-select Gemini 3.1 Pro in Gemini Enterprise web UI (aliases: 3.1pro, 3.1-pro)",
    ),
    _build_model_entry(
        "gemini-3.8-flash",
        "Auto-select Gemini 3.8 Flash in Gemini Enterprise web UI (aliases: 3.8flash, 3.8-flash)",
    ),
]

_EXTRA_RESOLVABLE_MODELS: List[Dict[str, Any]] = [
    _build_model_entry(
        "gemini-enterprise-web",
        "Gemini Enterprise / Agentspace (keep currently selected web model)",
    ),
    _build_model_entry(
        "gemini-enterprise-web-newchat",
        "Start a fresh web chat session using the currently selected web model",
    ),
    _build_model_entry(
        "gemini-4-carbon-newchat",
        "Start a fresh web chat session and select Gemini 4 Carbon",
    ),
    _build_model_entry(
        "gemini-4-hydrogen-newchat",
        "Start a fresh web chat session and select Gemini 4 Hydrogen",
    ),
    _build_model_entry(
        "gemini-4-argon-newchat",
        "Start a fresh web chat session and select Gemini 4 Argon",
    ),
    _build_model_entry(
        "gemini-3.1-pro-newchat",
        "Start a fresh web chat session and select Gemini 3.1 Pro",
    ),
    _build_model_entry(
        "gemini-3.8-flash-newchat",
        "Start a fresh web chat session and select Gemini 3.8 Flash",
    ),
    _build_model_entry(
        "gemini-2.5-pro",
        "Auto-select Gemini 2.5 Pro in Gemini Enterprise web UI (aliases: 2.5pro, 2.5-pro)",
    ),
    _build_model_entry(
        "gemini-2.5-pro-newchat",
        "Start a fresh web chat session and select Gemini 2.5 Pro",
    ),
    _build_model_entry(
        "gemini-2.5-flash",
        "Auto-select Gemini 2.5 Flash in Gemini Enterprise web UI (aliases: 2.5flash, 2.5-flash)",
    ),
    _build_model_entry(
        "gemini-2.5-flash-newchat",
        "Start a fresh web chat session and select Gemini 2.5 Flash",
    ),
    _build_model_entry(
        "gemini-enterprise-gcloud",
        "Official Google Cloud Vertex AI / Discovery Engine API via local gcloud ADC",
        owned_by="google-cloud-official",
    ),
]

_AVAILABLE_MODELS_BY_LOWER_ID: Dict[str, Dict[str, Any]] = {
    m["id"].lower(): m for m in [*AVAILABLE_MODELS, *_EXTRA_RESOLVABLE_MODELS]
}

_CANONICAL_TARGET_TO_MODEL_ID: Dict[str, str] = {
    "auto": "gemini-enterprise-web",
    "3.1-pro": "gemini-3.1-pro",
    "4-argon": "gemini-4-argon",
    "4-carbon": "gemini-4-carbon",
    "4-hydrogen": "gemini-4-hydrogen",
    "3.8-flash": "gemini-3.8-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.5-flash": "gemini-2.5-flash",
}


def format_gemini_display_name(canonical_or_target: str) -> str:
    """Format a canonical model ID or target key into a clean display label (e.g. `Gemini 4 Hydrogen`)."""
    raw = (canonical_or_target or "").strip()
    if not raw:
        return "Gemini Enterprise"
    low = raw.lower()
    for prefix in ("gemini-web:", "web:"):
        if low.startswith(prefix):
            return raw[len(prefix) :].strip() or "Gemini Enterprise"
    if low in {"gemini-enterprise-web", "gemini-enterprise-auto", "auto"}:
        return "Gemini Enterprise (Auto)"
    if low == "gemini-enterprise-gcloud":
        return "Gemini Enterprise (GCloud)"
    core = raw[7:].strip() if low.startswith("gemini-") else raw
    parts = [p for p in re.split(r"[\s_\-]+", core) if p]
    pretty_parts: List[str] = []
    for p in parts:
        if re.fullmatch(r"\d+(?:\.\d+)?", p):
            pretty_parts.append(p)
        else:
            pretty_parts.append(p.capitalize())
    joined = " ".join(pretty_parts) if pretty_parts else core
    return joined if joined.lower().startswith("gemini") else f"Gemini {joined}"


def normalize_web_label_to_model_spec(
    raw_label: Optional[str],
) -> Optional[tuple[str, str, str]]:
    """Convert a live browser dropdown label into `(canonical_model_id, web_target_key, display_name)`."""
    if not raw_label:
        return None
    headline = str(raw_label).splitlines()[0].strip()
    if not headline or len(headline) > 80 or _GENERIC_WEB_PLACEHOLDER_RE.match(headline):
        return None

    low = headline.lower()
    if low in _KEEP_CURRENT_WEB_MODEL_IDS:
        return None

    norm_hyphen = re.sub(r"[\s_]+", "-", low)
    norm_compact = re.sub(r"[\s_\-]+", "", low)
    alias_target = WEB_MODEL_ALIASES.get(norm_hyphen) or WEB_MODEL_ALIASES.get(norm_compact)
    if alias_target:
        if alias_target == "auto":
            return None
        cid = _CANONICAL_TARGET_TO_MODEL_ID.get(alias_target, f"gemini-{alias_target}")
        return (cid, alias_target, format_gemini_display_name(cid))

    dyn_target = _parse_dynamic_gemini_target(headline)
    if dyn_target:
        cid = _CANONICAL_TARGET_TO_MODEL_ID.get(dyn_target, f"gemini-{dyn_target}")
        return (cid, dyn_target, format_gemini_display_name(cid))

    # Arbitrary custom enterprise model/agent exposed in the web selector
    return (f"gemini-web:{headline}", headline, headline)


def get_dynamic_available_models(
    discovered_labels: Optional[List[str]] = None,
    extra_default_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return static `AVAILABLE_MODELS` merged with any newly discovered future models from the browser."""
    out: List[Dict[str, Any]] = [_clone_model_entry(m) for m in AVAILABLE_MODELS]
    seen_ids = {m["id"].lower() for m in out}

    candidates: List[str] = list(discovered_labels or [])
    if extra_default_model:
        base_def, _ = parse_model_and_session(extra_default_model)
        if base_def and base_def.lower().endswith("-newchat"):
            base_def = base_def[: -len("-newchat")].strip()
        if base_def and base_def.lower() not in seen_ids:
            candidates.append(base_def)

    for label in candidates:
        spec = normalize_web_label_to_model_spec(label)
        if not spec:
            continue
        cid, target_key, disp = spec
        if cid.lower() not in seen_ids:
            desc = (
                f"Auto-discovered {disp} in Gemini Enterprise web UI (target: {target_key})"
                if not cid.lower().startswith("gemini-web:")
                else f"Auto-discovered custom Gemini Enterprise web model ({disp})"
            )
            out.append(_build_model_entry(cid, desc))
            seen_ids.add(cid.lower())
        if not cid.lower().startswith("gemini-web:"):
            nc_id = f"{cid}-newchat"
            if nc_id.lower() not in seen_ids:
                out.append(
                    _build_model_entry(
                        nc_id,
                        f"Start a fresh web chat session and select {disp}",
                    )
                )
                seen_ids.add(nc_id.lower())
    return out


def _clone_model_entry(entry: Dict[str, Any], model_id: Optional[str] = None) -> Dict[str, Any]:
    cloned = dict(entry)
    cloned["capabilities"] = dict(entry.get("capabilities") or {})
    cloned["limits"] = dict(entry.get("limits") or {})
    if model_id is not None:
        cloned["id"] = model_id
    return cloned


def resolve_model_metadata(
    model_id: Optional[str],
    discovered_labels: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve `GET /v1/models/{model_id}` for canonical IDs, shorthand aliases, future models, and `@session` tags."""
    if not model_id:
        return None
    raw_id = model_id.strip()
    if not raw_id:
        return None

    direct = _AVAILABLE_MODELS_BY_LOWER_ID.get(raw_id.lower())
    if direct is not None:
        return _clone_model_entry(direct, model_id=raw_id if raw_id != direct["id"] else None)

    base_model, session_tag = parse_model_and_session(raw_id)
    if not base_model:
        return None

    direct_base = _AVAILABLE_MODELS_BY_LOWER_ID.get(base_model.lower())
    if direct_base is not None:
        out = _clone_model_entry(direct_base, model_id=raw_id)
        if session_tag:
            out["description"] = f"{direct_base['description']} [session: {session_tag}]"
        return out

    wants_new_chat = model_requests_new_chat(base_model)
    core_name = (
        base_model[: -len("-newchat")].strip() if wants_new_chat else base_model.strip()
    )
    core_name = _strip_provider_prefix_if_gemini(core_name)
    if not core_name:
        return None

    lowered_core = core_name.lower()
    if lowered_core in {"gcloud", "gemini-enterprise-gcloud"}:
        gcloud_base = _AVAILABLE_MODELS_BY_LOWER_ID["gemini-enterprise-gcloud"]
        return _clone_model_entry(gcloud_base, model_id=raw_id)

    if lowered_core in _LEGACY_DEFAULT_MODEL_IDS:
        target_key = (
            "gemini-enterprise-web-newchat" if wants_new_chat else "gemini-enterprise-web"
        )
        return _clone_model_entry(_AVAILABLE_MODELS_BY_LOWER_ID[target_key], model_id=raw_id)

    normalized_hyphen = re.sub(r"[\s_]+", "-", lowered_core)
    normalized_compact = re.sub(r"[\s_\-]+", "", lowered_core)
    alias_target = WEB_MODEL_ALIASES.get(normalized_hyphen) or WEB_MODEL_ALIASES.get(
        normalized_compact
    )
    if alias_target:
        canonical_id = _CANONICAL_TARGET_TO_MODEL_ID.get(alias_target, "gemini-enterprise-web")
        if wants_new_chat and f"{canonical_id}-newchat" in _AVAILABLE_MODELS_BY_LOWER_ID:
            canonical_id = f"{canonical_id}-newchat"
        base_entry = _AVAILABLE_MODELS_BY_LOWER_ID[canonical_id]
        out = _clone_model_entry(base_entry, model_id=raw_id)
        if session_tag:
            out["description"] = f"{base_entry['description']} [session: {session_tag}]"
        return out

    if lowered_core in {"gpt-4", "gpt-4o", "o1", "o3"} or lowered_core.startswith(
        _GENERIC_THIRD_PARTY_PREFIXES
    ):
        return None

    # Check live browser-discovered models (including custom enterprise models without `gemini-` prefix)
    if discovered_labels:
        for disc_raw in discovered_labels:
            spec = normalize_web_label_to_model_spec(disc_raw)
            if not spec:
                continue
            disc_cid, disc_target, disc_disp = spec
            if lowered_core in {disc_cid.lower(), disc_target.lower(), disc_disp.lower()}:
                desc = f"Auto-discovered Gemini Enterprise web model ({disc_disp})"
                if wants_new_chat:
                    desc += " [new chat]"
                if session_tag:
                    desc += f" [session: {session_tag}]"
                return _build_model_entry(raw_id, desc)

    dyn_target = _parse_dynamic_gemini_target(core_name)
    if dyn_target is not None or lowered_core.startswith(("gemini-web:", "web:", "gemini-")):
        resolved_label = dyn_target or resolve_target_web_model(core_name) or core_name
        disp = format_gemini_display_name(resolved_label)
        desc = f"Auto-adapted Gemini Enterprise web model ({disp})"
        if wants_new_chat:
            desc += " [new chat]"
        if session_tag:
            desc += f" [session: {session_tag}]"
        return _build_model_entry(raw_id, desc)

    return None

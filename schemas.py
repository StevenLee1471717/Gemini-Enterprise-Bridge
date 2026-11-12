"""OpenAI-compatible request/response schemas, model alias resolver, and token helpers."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field

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

    def text_content(self) -> str:
        return extract_text_content(self.content)

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
    model: str = Field(default="gemini-enterprise-web")
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


class BridgeRegisterRequest(_FlexibleModel):
    client_id: Optional[str] = None
    user_agent: Optional[str] = None
    tab_id: Optional[int] = None
    tab_url: Optional[str] = None
    tab_title: Optional[str] = None
    driver_mode: str = "auto"
    active_model: Optional[str] = None
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
    citations: Optional[List[Dict[str, Any]]] = None
    source: Optional[str] = None
    tab_url: Optional[str] = None
    tab_title: Optional[str] = None
    ready: Optional[bool] = None
    driver_mode: Optional[str] = None
    captured_rpc: Optional[bool] = None
    active_model: Optional[str] = None
    tabs: Optional[List[Dict[str, Any]]] = None
    capabilities: Optional[List[str]] = None
    chunk_index: Optional[int] = None
    total_chunks: Optional[int] = None
    data: Optional[str] = None


class BridgeCallbackPayload(_FlexibleModel):
    """Direct HTTP callback upload for large (>32KB / 100KB+) replies & tool calls."""

    client_id: Optional[str] = None
    tab_id: Optional[int] = None
    request_id: str
    reply: Optional[str] = None
    error: Optional[str] = None
    citations: Optional[List[Dict[str, Any]]] = None
    source: Optional[str] = "http_callback"
    active_model: Optional[str] = None


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
                header = f'[TOOL_RESULT call_id="{call_id}"]' if call_id else "[TOOL_RESULT]"
                parts.append(f"{header}\n{inner}\n[/TOOL_RESULT]")
            elif part_type == "image_url":
                parts.append("[Image attachment omitted in text bridge]")
        return "\n".join(p for p in parts if p)
    return str(content)


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    """Character-based token count approximation without JSON punctuation inflation."""
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub("", text)
    return max(1, int(cjk_count * 0.65 + len(non_cjk) / 4.2))


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

_KEEP_CURRENT_WEB_MODEL_IDS = {
    "",
    "default",
    "auto",
    "web",
    "gemini-enterprise",
    "gemini-enterprise-web",
    "gemini-enterprise-gcloud",
    "gcloud",
}

_GENERIC_THIRD_PARTY_PREFIXES = (
    "gpt-",
    "o1-",
    "o3-",
    "o4-",
    "claude-",
)


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


def resolve_target_web_model(model_name: Optional[str]) -> Optional[str]:
    """Resolve an OpenAI `model` parameter into a Gemini Enterprise web UI selector target.

    Returns:
        - `None` when the current browser model should be kept unchanged (e.g. `gemini-enterprise-web` or `gcloud`)
        - Canonical web model key (`"3.1-pro"`, `"4-argon"`, `"4-carbon"`, `"3.8-flash"`, `"2.5-pro"`, `"2.5-flash"`)
        - Or a clean custom UI label for pass-through models (`gemini-web:<label>` or future Gemini versions)
    """
    base_model, _ = parse_model_and_session(model_name)
    if not base_model:
        return None

    raw = base_model.strip()
    if raw.lower().endswith("-newchat"):
        raw = raw[: -len("-newchat")].strip()

    if not raw:
        return None

    lowered = raw.lower()
    if lowered in _KEEP_CURRENT_WEB_MODEL_IDS:
        return None

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
        "capabilities": {
            "chat": True,
            "tools": True,
            "function_calling": True,
            "streaming": True,
            "json_mode": True,
        },
        "limits": {
            "max_prompt_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "max_context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        },
    }


AVAILABLE_MODELS: List[Dict[str, Any]] = [
    _build_model_entry(
        "gemini-enterprise-web",
        "Gemini Enterprise / Agentspace (keep currently selected web model)",
    ),
    _build_model_entry(
        "gemini-enterprise-web-newchat",
        "Start a fresh web chat session using the currently selected web model",
    ),
    _build_model_entry(
        "gemini-3.1-pro",
        "Auto-select Gemini 3.1 Pro in Gemini Enterprise web UI (aliases: 3.1pro, 3.1-pro)",
    ),
    _build_model_entry(
        "gemini-3.1-pro-newchat",
        "Start a fresh web chat session and select Gemini 3.1 Pro",
    ),
    _build_model_entry(
        "gemini-4-argon",
        "Auto-select Gemini 4 Argon in Gemini Enterprise web UI (aliases: 4argon, 4-argon)",
    ),
    _build_model_entry(
        "gemini-4-argon-newchat",
        "Start a fresh web chat session and select Gemini 4 Argon",
    ),
    _build_model_entry(
        "gemini-4-carbon",
        "Auto-select Gemini 4 Carbon in Gemini Enterprise web UI (aliases: 4carbon, 4-carbon)",
    ),
    _build_model_entry(
        "gemini-4-carbon-newchat",
        "Start a fresh web chat session and select Gemini 4 Carbon",
    ),
    _build_model_entry(
        "gemini-3.8-flash",
        "Auto-select Gemini 3.8 Flash in Gemini Enterprise web UI (aliases: 3.8flash, 3.8-flash)",
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
    m["id"].lower(): m for m in AVAILABLE_MODELS
}

_CANONICAL_TARGET_TO_MODEL_ID: Dict[str, str] = {
    "auto": "gemini-enterprise-web",
    "3.1-pro": "gemini-3.1-pro",
    "4-argon": "gemini-4-argon",
    "4-carbon": "gemini-4-carbon",
    "3.8-flash": "gemini-3.8-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.5-flash": "gemini-2.5-flash",
}


def _clone_model_entry(entry: Dict[str, Any], model_id: Optional[str] = None) -> Dict[str, Any]:
    cloned = dict(entry)
    cloned["capabilities"] = dict(entry.get("capabilities") or {})
    cloned["limits"] = dict(entry.get("limits") or {})
    if model_id is not None:
        cloned["id"] = model_id
    return cloned


def resolve_model_metadata(model_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve `GET /v1/models/{model_id}` for canonical IDs, shorthand aliases, and `@session` tags."""
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
    if not core_name:
        return None

    lowered_core = core_name.lower()
    if lowered_core in {"gcloud", "gemini-enterprise-gcloud"}:
        gcloud_base = _AVAILABLE_MODELS_BY_LOWER_ID["gemini-enterprise-gcloud"]
        return _clone_model_entry(gcloud_base, model_id=raw_id)

    if lowered_core in _KEEP_CURRENT_WEB_MODEL_IDS:
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

    if lowered_core.startswith(("gemini-web:", "web:", "gemini-")):
        resolved_label = resolve_target_web_model(core_name) or core_name
        desc = f"Custom Gemini Enterprise web model ({resolved_label})"
        if wants_new_chat:
            desc += " [new chat]"
        if session_tag:
            desc += f" [session: {session_tag}]"
        return _build_model_entry(raw_id, desc)

    return None

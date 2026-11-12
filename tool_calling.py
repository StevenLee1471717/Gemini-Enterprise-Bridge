"""DOM-safe OpenAI Tool Calling, Response Format, and Live Stream Parser for Gemini Enterprise Bridge."""

from __future__ import annotations

import ast
import json
import re
import shlex
import uuid
from web_text import WebTextFilter, clean_web_text
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOL_CALL_OPEN = "[TOOL_CALL]"
TOOL_CALL_CLOSE = "[/TOOL_CALL]"


class ToolProtocolError(ValueError):
    """A tool-enabled reply cannot safely be delivered or executed."""


TOOL_OUTPUT_REPAIR_PROMPT = """The previous reply could not be accepted: it contained an incomplete or
unrecognized tool call, or internal thinking instead of a user-facing response.
No tool from that reply was executed. Continue the current task from the latest
real tool results. Reissue the intended call with the EXACT declared tool name
and a complete valid JSON arguments object inside [TOOL_CALL]...[/TOOL_CALL],
then stop. If no tool is needed, give the user the actual answer. Do not output
internal thinking, analysis transcripts, examples of tool calls, or repeat old work."""

_THINKING_PREFIXES = (
    "thinking process:",
    "thinking process：",
    "思考过程:",
    "思考过程：",
    "思维过程:",
    "思维过程：",
    "内部思考:",
    "内部思考：",
    "<think>",
    "<analysis>",
)
_THINKING_HEADER_BOLD_RE = re.compile(
    r"^((?:thinking process|思考过程|思维过程|内部思考))\*+(?=[:：]|$)",
    re.IGNORECASE,
)


def _thinking_prefix(text: str) -> str:
    stripped = clean_web_text(text).lstrip().lstrip("*#>\t ")
    if stripped.endswith("*"):
        stripped = stripped.rstrip("*")
    stripped = _THINKING_HEADER_BOLD_RE.sub(r"\1", stripped)
    return stripped.lower()


_FENCED_TOOL_RE = re.compile(
    r"```(?:tool_call|tool_use)\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)
_FENCED_JSON_WRAPPER_RE = re.compile(
    r"```(?:json)?\s*\n?(\{\s*\"tool_calls\"\s*:\s*\[.*?\]\s*\})\s*\n?```",
    re.DOTALL | re.IGNORECASE,
)
_EMPTY_CODE_FENCE_RE = re.compile(
    r"```(?:json|tool_call|tool_use|xml|text)?\s*```",
    re.IGNORECASE,
)
_GENERIC_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ParsedAssistantTurn:
    """Result of parsing a raw model reply for OpenAI tool calls and content."""

    content: Optional[str]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.has_tool_calls else "stop"


def normalize_request_tools(
    tools: Optional[Sequence[Dict[str, Any]]],
    functions: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Normalize OpenAI `tools` and legacy `functions` into canonical OpenAI function tool specs."""
    normalized: List[Dict[str, Any]] = []
    if tools:
        for item in tools:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function" and isinstance(item.get("function"), dict):
                fn = item["function"]
                if fn.get("name"):
                    normalized.append({"type": "function", "function": fn})
            elif item.get("name"):
                # Tolerate flat function spec passed inside `tools`
                normalized.append({"type": "function", "function": item})
    elif functions:
        for fn in functions:
            if isinstance(fn, dict) and fn.get("name"):
                normalized.append({"type": "function", "function": fn})
    return normalized


def normalize_tool_choice(
    tool_choice: Optional[Any],
    function_call: Optional[Any] = None,
) -> Optional[Any]:
    """Normalize `tool_choice` and legacy `function_call`."""
    choice = tool_choice if tool_choice is not None else function_call
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice.strip().lower()
    if isinstance(choice, dict):
        if choice.get("type") == "function" and isinstance(choice.get("function"), dict):
            return choice
        if isinstance(choice.get("name"), str) and choice["name"].strip():
            return {"type": "function", "function": {"name": choice["name"].strip()}}
    return choice


def get_forced_function_name(tool_choice: Optional[Any]) -> Optional[str]:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"].strip() or None
    return None


def is_tool_generation_enabled(
    tools: Sequence[Dict[str, Any]],
    tool_choice: Optional[Any],
) -> bool:
    if not tools:
        return False
    if isinstance(tool_choice, str) and tool_choice.strip().lower() == "none":
        return False
    return True


def compute_tools_fingerprint(tools: Sequence[Dict[str, Any]]) -> str:
    """Deterministic fingerprint so unchanged tool schemas aren't redundantly repeated every turn."""
    if not tools:
        return ""
    try:
        return json.dumps(tools, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(tools)


def build_tool_system_instructions(
    tools: Sequence[Dict[str, Any]],
    tool_choice: Optional[Any] = None,
    parallel_tool_calls: Optional[bool] = True,
) -> str:
    """Format tool declarations and DOM-safe `[TOOL_CALL]...[/TOOL_CALL]` protocol instructions."""
    if not is_tool_generation_enabled(tools, tool_choice):
        return ""

    tool_specs: List[Dict[str, Any]] = []
    for t in tools:
        fn = t.get("function") or {}
        raw_params = fn.get("parameters") or {"type": "object", "properties": {}}
        spec: Dict[str, Any] = {
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": raw_params,
        }
        if fn.get('name') in {'read_thread', 'mcp__codex_app.read_thread'} and 'threadId' in raw_params.get('properties', {}):
            spec['parameters'] = json.loads(json.dumps(raw_params))
            for key, limit in [('turnLimit', 10), ('maxOutputCharsPerItem', 20000)]:
                field = spec['parameters']['properties'].get(key)
                if isinstance(field, dict):
                    field['maximum'] = min(field.get('maximum', limit), limit)
            spec['description'] += '\nRead at most 10 turns and 20000 characters per item. For orientation start with turnLimit=2, maxOutputCharsPerItem=1000 and includeOutputs=false; expand only when needed and use cursor for older turns.'
        tool_specs.append(spec)

    # Use compact JSON automatically when tool schemas are large (>8 KB) to save context window
    pretty_json = json.dumps(tool_specs, ensure_ascii=False, indent=2)
    tools_json = (
        json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":"))
        if len(pretty_json) > 8000
        else pretty_json
    )
    forced_fn = get_forced_function_name(tool_choice)
    choice_str = tool_choice if isinstance(tool_choice, str) else ""

    rules: List[str] = [
        "[Tool Calling Protocol]",
        "You have access to the following external tools (functions):",
        tools_json,
        "",
        "When you decide to invoke a tool, emit the call using these EXACT plain-text delimiters (do NOT wrap them in markdown links or alter the bracket names):",
        f'{TOOL_CALL_OPEN}{{"name": "function_name", "arguments": {{"param": "value"}}}}{TOOL_CALL_CLOSE}',
    ]

    if parallel_tool_calls is False:
        rules.append(
            "- Emit at most ONE `[TOOL_CALL]...[/TOOL_CALL]` block in this turn, then wait for the `[TOOL_RESULT]`."
        )
    else:
        rules.append(
            "- If multiple independent tool calls are needed at once, emit multiple `[TOOL_CALL]...[/TOOL_CALL]` blocks back-to-back."
        )

    rules.extend(
        [
            "- `arguments` MUST be a valid JSON object matching the tool's `parameters` schema.",
            "- Output only brief user-facing progress, tool calls, or the final answer. Never output an internal thinking transcript or a 'Thinking Process:' / '思考过程：' section.",
            "- Never fabricate `[TOOL_RESULT]` yourself; stop immediately after emitting your `[TOOL_CALL]...[/TOOL_CALL]` block(s) without stacking extra waiting/submitted status sentences, so the client can execute the tool and return `[TOOL_RESULT]`.",
            "- When no tool call is needed (or once `[TOOL_RESULT]` gives enough information), answer the user normally WITHOUT any `[TOOL_CALL]` tags.",
        ]
    )

    if forced_fn:
        rules.append(
            f'- MANDATORY FOR THIS TURN: You MUST call the function "{forced_fn}" using {TOOL_CALL_OPEN}{{"name": "{forced_fn}", "arguments": {{...}}}}{TOOL_CALL_CLOSE}.'
        )
    elif choice_str == "required":
        rules.append(
            "- MANDATORY FOR THIS TURN: You MUST invoke at least one declared tool using `[TOOL_CALL]...[/TOOL_CALL]`."
        )

    return "\n".join(rules)


def build_tool_delta_reminder(
    tools: Sequence[Dict[str, Any]],
    tool_choice: Optional[Any] = None,
    has_tool_results: bool = False,
) -> str:
    """Compact per-turn instruction for incremental multi-turn agentic continuation."""
    if isinstance(tool_choice, str) and tool_choice == "none":
        return "Do NOT invoke any tools in this turn; provide your response directly to the user."

    if not tools:
        return ""

    forced_fn = get_forced_function_name(tool_choice)
    if forced_fn:
        return (
            f'You MUST now call tool "{forced_fn}" using '
            f'{TOOL_CALL_OPEN}{{"name": "{forced_fn}", "arguments": {{...}}}}{TOOL_CALL_CLOSE}.'
        )
    if isinstance(tool_choice, str) and tool_choice == "required":
        return (
            "You MUST invoke at least one declared tool in this turn using "
            f'{TOOL_CALL_OPEN}{{"name": "...", "arguments": {{...}}}}{TOOL_CALL_CLOSE}.'
        )
    if has_tool_results:
        return (
            "Continue using the `[TOOL_RESULT]` output(s) above. "
            f'If another tool step is needed, emit {TOOL_CALL_OPEN}{{"name": "...", "arguments": {{...}}}}{TOOL_CALL_CLOSE} '
            "and stop; otherwise provide the final answer directly without `[TOOL_CALL]` tags."
        )
    return ""


def build_response_format_instructions(response_format: Optional[Dict[str, Any]]) -> str:
    """Generate prompt instructions for OpenAI `response_format` (`json_object` / `json_schema`)."""
    if not isinstance(response_format, dict):
        return ""
    fmt_type = str(response_format.get("type") or "").strip().lower()
    if fmt_type == "json_object":
        return (
            "[Output Format Requirement]\n"
            "Return ONLY a single valid JSON object. Do not include markdown code fences or explanatory prose outside the JSON object."
        )
    if fmt_type == "json_schema":
        schema_Spec = response_format.get("json_schema") or {}
        schema_obj = schema_Spec.get("schema") if isinstance(schema_Spec, dict) else None
        schema_str = (
            json.dumps(schema_obj, ensure_ascii=False, indent=2)
            if schema_obj
            else "{}"
        )
        return (
            "[Output Format Requirement]\n"
            "Return ONLY a single valid JSON value conforming strictly to this JSON Schema (no markdown fences or extra prose):\n"
            f"{schema_str}"
        )
    return ""


def format_assistant_history_entry(
    text_content: str,
    tool_calls: Sequence[Dict[str, Any]],
) -> str:
    """Canonical representation of an assistant turn (text + tool calls) for history matching & context."""
    parts: List[str] = []
    cleaned_text = (text_content or "").strip()
    if cleaned_text:
        parts.append(cleaned_text)

    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = str(fn.get("name") or tc.get("name") or "").strip()
        raw_args = fn.get("arguments", tc.get("arguments", "{}"))
        args_obj = _coerce_args_to_obj(raw_args)
        canonical_payload = {
            "id": str(tc.get("id") or ""),
            "name": name,
            "arguments": args_obj,
        }
        parts.append(
            f"{TOOL_CALL_OPEN}{json.dumps(canonical_payload, sort_keys=True, ensure_ascii=False)}{TOOL_CALL_CLOSE}"
        )

    return "\n".join(parts).strip()


def format_tool_result_entry(
    content_text: str,
    tool_call_id: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    attrs: List[str] = []
    if tool_call_id:
        attrs.append(f'call_id="{tool_call_id}"')
    if name:
        attrs.append(f'name="{name}"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    body = (content_text or "").strip() or "(empty result)"
    return f"[TOOL_RESULT{attr_str}]\n{body}\n[/TOOL_RESULT]"


# ---------------------------------------------------------------------------
# Tolerant Multi-Layer Tool Call Parser
# ---------------------------------------------------------------------------


def _coerce_args_to_obj(raw_args: Any) -> Any:
    if raw_args is None:
        return {}
    if isinstance(raw_args, (dict, list)):
        return raw_args
    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            try:
                val = ast.literal_eval(s)
                if isinstance(val, (dict, list)):
                    return val
            except Exception:
                pass
            return {"_raw": s}
    return {"value": raw_args}


def _coerce_args_to_json_str(raw_args: Any) -> str:
    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return "{}"
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
            return json.dumps({"value": parsed}, ensure_ascii=False)
        except json.JSONDecodeError:
            obj = _coerce_args_to_obj(s)
            return json.dumps(obj, ensure_ascii=False)
    obj = _coerce_args_to_obj(raw_args)
    return json.dumps(obj, ensure_ascii=False)


def _try_parse_json_loose(raw_snippet: str) -> Optional[Any]:
    cleaned = raw_snippet.strip()
    if cleaned.startswith("```"):
        m = _GENERIC_JSON_FENCE_RE.search(cleaned)
        if m:
            cleaned = m.group(1).strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Extract balanced {...} or [...] inside snippet
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            sub = cleaned[start : end + 1]
            try:
                return json.loads(sub)
            except json.JSONDecodeError:
                try:
                    val = ast.literal_eval(sub)
                    if isinstance(val, (dict, list)):
                        return val
                except Exception:
                    pass
    return None


def _tool_blocks(text: str, opening: str, closing: str):
    """Find protocol boundaries outside JSON/Python string literals."""
    marker = re.compile(re.escape(opening), re.IGNORECASE)
    lower = text.lower()
    position = 0
    while match := marker.search(text, position):
        index = match.end()
        quote = None
        escaped = False
        while index < len(text):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif lower.startswith(closing.lower(), index):
                break
            index += 1
        end = min(index + len(closing), len(text))
        yield match.start(), end, text[match.end():index]
        position = end


def _candidates_from_parsed_json(
    parsed: Any,
    allowed_map: Dict[str, str],
    forced_fn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            results.extend(_candidates_from_parsed_json(item, allowed_map, forced_fn))
        return results

    if not isinstance(parsed, dict):
        return results

    if isinstance(parsed.get("tool_calls"), list):
        for item in parsed["tool_calls"]:
            results.extend(_candidates_from_parsed_json(item, allowed_map, forced_fn))
        return results

    raw_name = ""
    raw_args: Any = None

    if isinstance(parsed.get("function"), dict):
        fn_obj = parsed["function"]
        raw_name = str(fn_obj.get("name") or "").strip()
        raw_args = fn_obj.get("arguments", fn_obj.get("parameters", {}))
    elif isinstance(parsed.get("name"), str):
        raw_name = parsed["name"].strip()
        if "arguments" in parsed:
            raw_args = parsed["arguments"]
        elif "parameters" in parsed:
            raw_args = parsed["parameters"]
        elif "input" in parsed:
            raw_args = parsed["input"]
        else:
            # Flat object with {"name": "fn", ...other keys as args}
            extra = {k: v for k, v in parsed.items() if k not in {"name", "id", "type"}}
            raw_args = extra
    elif isinstance(parsed.get("tool"), str):
        raw_name = parsed["tool"].strip()
        raw_args = parsed.get("arguments", parsed.get("parameters", parsed.get("input", {})))
    elif forced_fn and forced_fn.lower() in allowed_map:
        # Forced single function where the model emitted the arguments JSON object directly
        raw_name = allowed_map[forced_fn.lower()]
        raw_args = parsed

    if not raw_name:
        return results

    canonical_name = allowed_map.get(raw_name.lower())
    if not canonical_name and "." not in raw_name:
        # Codex can add a namespace after the web conversation has already started.
        aliases = [name for key, name in allowed_map.items() if key.rsplit(".", 1)[-1] == raw_name.lower()]
        if len(aliases) == 1:
            canonical_name = aliases[0]
    if not canonical_name:
        return results

    if forced_fn and canonical_name.lower() != forced_fn.lower():
        return results

    call_id = (
        str(parsed.get("id")).strip()
        if isinstance(parsed.get("id"), str) and parsed["id"].strip()
        else f"call_{uuid.uuid4().hex[:24]}"
    )

    results.append(
        {
            "id": call_id,
            "type": "function",
            "function": {
                "name": canonical_name,
                "arguments": _coerce_args_to_json_str(raw_args),
            },
        }
    )
    return results


def parse_assistant_output(
    raw_text: str,
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    parallel_tool_calls: Optional[bool] = True,
    response_format: Optional[Dict[str, Any]] = None,
) -> ParsedAssistantTurn:
    """Parse raw web/upstream reply into OpenAI `content` + `tool_calls`."""
    text = raw_text or ""
    norm_tools = list(tools or [])

    if _thinking_prefix(text).startswith(_THINKING_PREFIXES):
        raise ToolProtocolError("网页返回了内部思考文字，正在请求实际操作或答复。")

    if not is_tool_generation_enabled(norm_tools, tool_choice):
        cleaned = clean_json_response(text if response_format else clean_web_text(text), response_format)
        return ParsedAssistantTurn(content=cleaned, tool_calls=[])

    allowed_map: Dict[str, str] = {}
    for t in norm_tools:
        fn = t.get("function") or {}
        fn_name = str(fn.get("name") or "").strip()
        if fn_name:
            allowed_map[fn_name.lower()] = fn_name

    forced_fn = get_forced_function_name(tool_choice)
    extracted_calls: List[Dict[str, Any]] = []
    remaining_text = text

    # A closing tag in a patch or command is data, not a protocol boundary.
    for opening, closing in ((TOOL_CALL_OPEN, TOOL_CALL_CLOSE), ("<tool_call>", "</tool_call>")):
        pieces, cursor = [], 0
        for start, end, snippet in _tool_blocks(remaining_text, opening, closing):
            pieces.append(remaining_text[cursor:start])
            parsed = _try_parse_json_loose(snippet)
            calls = _candidates_from_parsed_json(parsed, allowed_map, forced_fn)
            if calls:
                extracted_calls.extend(calls)
            else:
                pieces.append(remaining_text[start:end])
            cursor = end
        remaining_text = "".join(pieces) + remaining_text[cursor:]

    # Layer 3: Fenced ```tool_call ... ``` or ```json {"tool_calls": [...]} ```
    if not extracted_calls:
        for pattern in (_FENCED_TOOL_RE, _FENCED_JSON_WRAPPER_RE):
            def _replace_fenced(match: re.Match[str]) -> str:
                parsed = _try_parse_json_loose(match.group(1))
                calls = _candidates_from_parsed_json(parsed, allowed_map, forced_fn)
                if calls:
                    extracted_calls.extend(calls)
                    return ""
                return match.group(0)

            remaining_text = pattern.sub(_replace_fenced, remaining_text)
            if extracted_calls:
                break

    # Layer 4: Bare JSON fallback when `tool_choice` is required/forced (or entire reply is a tool JSON)
    is_forced_mode = bool(forced_fn) or (isinstance(tool_choice, str) and tool_choice == "required")
    if not extracted_calls:
        stripped_whole = remaining_text.strip()
        looks_pure_json = (
            stripped_whole.startswith("{")
            or stripped_whole.startswith("[")
            or stripped_whole.startswith("```")
        )
        if is_forced_mode or looks_pure_json:
            parsed = _try_parse_json_loose(stripped_whole)
            # For non-forced pure JSON, require explicit `name` + `arguments` (or `tool_calls`) to avoid false positives
            if parsed is not None and (
                is_forced_mode
                or (
                    isinstance(parsed, dict)
                    and (
                        ("name" in parsed and "arguments" in parsed)
                        or "tool_calls" in parsed
                    )
                )
            ):
                calls = _candidates_from_parsed_json(parsed, allowed_map, forced_fn)
                if calls:
                    extracted_calls.extend(calls)
                    remaining_text = ""

    if re.search(r"\[TOOL_CALL\]|<tool_call>", remaining_text, re.IGNORECASE):
        raise ToolProtocolError("网页工具请求不完整或名称无法识别，未执行该请求。")

    if not extracted_calls:
        cleaned = clean_json_response(text if response_format else clean_web_text(text), response_format)
        return ParsedAssistantTurn(content=cleaned, tool_calls=[])

    extracted_calls = _normalize_query_calls(extracted_calls)
    if parallel_tool_calls is False and len(extracted_calls) > 1:
        extracted_calls = extracted_calls[:1]

    # Clean leftover empty markdown code fences around stripped `[TOOL_CALL]` blocks
    cleaned_preamble = clean_web_text(_EMPTY_CODE_FENCE_RE.sub("", remaining_text)).strip()
    return ParsedAssistantTurn(
        content=cleaned_preamble if cleaned_preamble else None,
        tool_calls=extracted_calls,
    )


def _is_read_query(call: dict) -> bool:
    fn = call['function']
    name = fn['name'].rsplit('.', 1)[-1]
    if name in {'read_thread', 'read_file', 'read_text_file'}:
        return True
    if name != 'exec_command':
        return False
    args = json.loads(fn['arguments'])
    if not isinstance(args, dict):
        return False
    cmd = args.get('cmd', '')
    if not isinstance(cmd, str) or re.search(r'[;&<>`$\n]', cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    # Only plain file reads and the common numbered-line pipeline are unambiguous.
    if tokens and tokens[0] == 'cat' and '|' not in tokens:
        return True
    return (len(tokens) == 7 and tokens[:2] == ['nl', '-ba'] and tokens[3:6] == ['|', 'sed', '-n']
            and bool(re.fullmatch(r'\d+(?:,\d+)?p', tokens[6])))


def _normalize_query_calls(calls: list[dict]) -> list[dict]:
    for call in calls:
        fn = call['function']
        if fn['name'] in {'read_thread', 'mcp__codex_app.read_thread'}:
            args = json.loads(fn['arguments'])
            if isinstance(args, dict) and 'threadId' in args:
                for key, limit in [('turnLimit', 10), ('maxOutputCharsPerItem', 20000)]:
                    if type(args.get(key)) is int and args[key] > limit:
                        args[key] = limit
                fn['arguments'] = json.dumps(args, ensure_ascii=False)
    if not all(_is_read_query(call) for call in calls):
        return calls
    seen, unique = set(), []
    for call in calls:
        fn = call['function']
        key = (fn['name'], json.dumps(json.loads(fn['arguments']), sort_keys=True, ensure_ascii=False))
        if key not in seen:
            unique.append(call)
            seen.add(key)
    return unique


def clean_json_response(text: str, response_format: Optional[Dict[str, Any]]) -> str:
    """Strip outer Markdown code fences when the client requested `json_object` or `json_schema`."""
    if not isinstance(response_format, dict):
        return text.strip()
    fmt_type = str(response_format.get("type") or "").strip().lower()
    if fmt_type not in {"json_object", "json_schema"}:
        return text.strip()

    stripped = text.strip()
    fence_match = _GENERIC_JSON_FENCE_RE.search(stripped)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            candidate = stripped[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
    return stripped


# ---------------------------------------------------------------------------
# Live SSE Streaming Guard & Tool Call Buffer
# ---------------------------------------------------------------------------

_GUARD_PREFIX_PATTERNS: Tuple[str, ...] = (
    "[TOOL_CALL]",
    "<tool_call>",
    "```tool_call",
    "```tool_use",
)


class ToolStreamProcessor:
    """Streams safe preamble `delta.content` chunks while holding back `[TOOL_CALL]` blocks."""

    def __init__(
        self,
        tools: Sequence[Dict[str, Any]],
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tools = list(tools)
        self.tool_choice = tool_choice
        self.parallel_tool_calls = parallel_tool_calls
        self.response_format = response_format
        self.tools_enabled = is_tool_generation_enabled(self.tools, self.tool_choice)
        self.forced_mode = bool(get_forced_function_name(tool_choice)) or (
            isinstance(tool_choice, str) and tool_choice == "required"
        )
        self.json_mode = isinstance(response_format, dict) and str(
            response_format.get("type") or ""
        ).lower() in {"json_object", "json_schema"}

        self._web_text = WebTextFilter()
        self._raw_parts: List[str] = []
        self._pending: str = ""
        self._capturing_tool: bool = False
        self._emitted_text_len: int = 0
        self._preamble_buffer = ""
        self._preamble_checked = False
        self._suppress_thinking = False
        self._emitted_parts: List[str] = []

    def feed(self, delta: str) -> List[str]:
        pieces = self._feed(delta)
        self._emitted_parts.extend(pieces)
        return pieces

    def _feed(self, delta: str) -> List[str]:
        """Process incoming chunk and return safe `delta.content` strings to emit right now."""
        if not delta:
            return []
        self._raw_parts.append(delta)
        delta = self._web_text.feed(delta)
        if not delta:
            return []
        if self._suppress_thinking:
            return []
        if not self._preamble_checked:
            self._preamble_buffer += delta
            prefix = _thinking_prefix(self._preamble_buffer)
            if prefix.startswith(_THINKING_PREFIXES):
                self._suppress_thinking = True
                return []
            if not prefix or any(marker.startswith(prefix) for marker in _THINKING_PREFIXES):
                return []
            self._preamble_checked = True
            delta, self._preamble_buffer = self._preamble_buffer, ""

        # If neither tools nor JSON-unwrapping mode is active, pass through with zero buffering
        if not self.tools_enabled and not self.json_mode:
            self._emitted_text_len += len(delta)
            return [delta]

        # In forced tool mode or strict JSON mode (where we strip markdown fences), buffer cleanly
        if self.forced_mode or self.json_mode:
            return []

        if self._capturing_tool:
            return []

        self._pending += delta
        upper_pending = self._pending.upper()

        # Check if a full tool-call opener has appeared in `_pending`
        earliest_hit: Optional[int] = None
        for marker in ("[TOOL_CALL]", "<TOOL_CALL>", "```TOOL_CALL", "```TOOL_USE"):
            pos = upper_pending.find(marker)
            if pos != -1 and (earliest_hit is None or pos < earliest_hit):
                earliest_hit = pos

        # Also check if `[TOOL_CALL]` was preceded immediately by a ```json fence
        if earliest_hit is not None:
            fence_before = self._pending.rfind("```", 0, earliest_hit)
            if fence_before != -1 and (earliest_hit - fence_before) <= 16:
                between = self._pending[fence_before:earliest_hit].strip()
                if between in {"```", "```json", "```xml", "```text"}:
                    earliest_hit = fence_before

            safe_prefix = self._pending[:earliest_hit]
            self._pending = self._pending[earliest_hit:]
            self._capturing_tool = True
            if safe_prefix:
                self._emitted_text_len += len(safe_prefix)
                return [safe_prefix]
            return []

        # Hold back any trailing suffix that could be the start of `[TOOL_CALL]`, `<tool_call>`, or ` ``` `
        hold_from = self._find_potential_marker_tail(self._pending)
        if hold_from == 0:
            return []
        if hold_from is not None and hold_from < len(self._pending):
            to_emit = self._pending[:hold_from]
            self._pending = self._pending[hold_from:]
            if to_emit:
                self._emitted_text_len += len(to_emit)
                return [to_emit]
            return []

        to_emit = self._pending
        self._pending = ""
        self._emitted_text_len += len(to_emit)
        return [to_emit]

    @staticmethod
    def _find_potential_marker_tail(buf: str) -> Optional[int]:
        """Return the earliest index near the end of `buf` that might grow into a tool marker."""
        upper = buf.upper()
        n = len(upper)

        # If there is a recent ` ``` ` within the last 24 chars without a closing newline+non-marker yet,
        # hold from ` ``` ` briefly so ` ```json\n[TOOL_CALL] ` never leaks ` ```json `.
        fence_pos = upper.rfind("```")
        if fence_pos != -1 and (n - fence_pos) <= 24:
            after_fence = upper[fence_pos + 3 :]
            # If we already see a normal language + newline + normal char (not `[` or `<` or `{`), it's safe!
            nl = after_fence.find("\n")
            if nl == -1:
                return fence_pos
            rest = after_fence[nl + 1 :].lstrip()
            if not rest or "[TOOL_CALL]".startswith(rest) or "<TOOL_CALL>".startswith(rest) or rest.startswith("{"):
                return fence_pos

        # Check suffixes of length 1..12 matching prefixes of `[TOOL_CALL]`, `<TOOL_CALL>`, or ` ``` `
        max_k = min(n, 12)
        for k in range(max_k, 0, -1):
            tail = upper[n - k :]
            for pattern in _GUARD_PREFIX_PATTERNS:
                if pattern.upper().startswith(tail):
                    return n - k
            if "```".startswith(tail):
                return n - k
        return None

    def finalize(self) -> Tuple[List[str], ParsedAssistantTurn]:
        tail, parsed = self._finalize()
        emitted = ''.join(self._emitted_parts)
        if parsed.has_tool_calls and not self.forced_mode and parsed.content and parsed.content.startswith(emitted):
            tail = [parsed.content[len(emitted):]]
        # History must reflect the text delivered to the client, including held preambles.
        parsed.content = (emitted + ''.join(tail)).strip() or (None if parsed.has_tool_calls else '')
        return tail, parsed

    def _finalize(self) -> Tuple[List[str], ParsedAssistantTurn]:
        """Finalize stream, returning `(remaining_content_deltas, parsed_turn)`."""
        self._pending += self._preamble_buffer + self._web_text.finish()
        self._preamble_buffer = ""
        full_raw = "".join(self._raw_parts)
        parsed = parse_assistant_output(
            full_raw,
            tools=self.tools,
            tool_choice=self.tool_choice,
            parallel_tool_calls=self.parallel_tool_calls,
            response_format=self.response_format,
        )

        if parsed.has_tool_calls:
            extra_deltas: List[str] = []
            # If forced mode buffered a brief preamble, or if safe preamble wasn't flushed yet
            if self.forced_mode and parsed.content:
                extra_deltas.append(parsed.content)
            return extra_deltas, parsed

        # No tool calls detected -> flush remaining buffered text cleanly
        if self.forced_mode or self.json_mode:
            final_text = parsed.content or ""
            return ([final_text] if final_text else []), parsed

        leftover = self._pending
        self._pending = ""
        return ([leftover] if leftover else []), parsed


# ---------------------------------------------------------------------------
# Large Payload & SSE Safe Chunking Helpers (Prevents 64KB Line / 1MB WS Truncation)
# ---------------------------------------------------------------------------

SSE_SAFE_SLICE_CHARS = 12288


def split_text_slices(text: str, slice_chars: int = SSE_SAFE_SLICE_CHARS) -> List[str]:
    """Split a potentially large string into bounded slices without losing characters."""
    if not text:
        return []
    if len(text) <= slice_chars:
        return [text]
    return [text[i : i + slice_chars] for i in range(0, len(text), slice_chars)]


def build_sse_tool_call_deltas(
    tool_calls: Sequence[Dict[str, Any]],
    max_arg_slice: int = SSE_SAFE_SLICE_CHARS,
) -> List[List[Dict[str, Any]]]:
    """Build OpenAI SSE `delta.tool_calls` payloads, slicing oversized `arguments` safely.

    For normal-sized tool calls (`<= max_arg_slice`), emits a single batch containing all tool calls.
    For 100KB+ tool call arguments, streams `id + name` first followed by `max_arg_slice` argument deltas
    so no single `data: {...}\\n\\n` SSE frame ever exceeds client/proxy 64KB line buffers.
    """
    if not tool_calls:
        return []

    total_arg_len = sum(len(str((tc.get("function") or {}).get("arguments") or "")) for tc in tool_calls)
    if total_arg_len <= max_arg_slice:
        return [
            [
                {
                    "index": idx,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for idx, tc in enumerate(tool_calls)
            ]
        ]

    batches: List[List[Dict[str, Any]]] = []
    for idx, tc in enumerate(tool_calls):
        fn_name = tc["function"]["name"]
        args_str = str(tc["function"]["arguments"] or "")
        slices = split_text_slices(args_str, max_arg_slice) or [""]
        batches.append(
            [
                {
                    "index": idx,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": slices[0],
                    },
                }
            ]
        )
        for extra_slice in slices[1:]:
            batches.append(
                [
                    {
                        "index": idx,
                        "function": {
                            "arguments": extra_slice,
                        },
                    }
                ]
            )
    return batches

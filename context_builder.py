"""Multi-turn prompt construction, model tracking, and agentic conversation delta tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import time
from schemas import ChatMessage, estimate_tokens, model_requests_new_chat, resolve_target_web_model
from usage_stats import DEFAULT_CONTEXT_WINDOW, get_context_window_for_model
from tool_calling import (
    build_response_format_instructions,
    build_tool_delta_reminder,
    build_tool_system_instructions,
    compute_tools_fingerprint,
    format_assistant_history_entry,
    format_tool_result_entry,
    is_tool_generation_enabled,
)

_CALL_ID_IN_CANON_RE = re.compile(r'"id":\s*"[^"]*",\s*')
_EPHEMERAL_WRAPPER_RE = re.compile(
    r"<(?P<tag>system_reminder|system-reminder|current_datetime|app_user_message_source_json|canvas-context|environment_details|context_info)\b[^>]*>[\s\S]*?</(?P=tag)>",
    re.IGNORECASE,
)
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _canonicalize_for_match(role: str, text: str) -> str:
    """Strip volatile per-turn client metadata tags and normalize whitespace for prefix comparison."""
    if not text:
        return ""
    cleaned = _EPHEMERAL_WRAPPER_RE.sub("", text)
    if role == "assistant" and "[TOOL_CALL]" in cleaned:
        cleaned = _CALL_ID_IN_CANON_RE.sub("", cleaned)
    return _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()


@dataclass
class PreparedPrompt:
    prompt: str
    reset_chat: bool
    strategy_used: str
    normalized_history: List[Tuple[str, str]]
    target_model: Optional[str] = None
    tools_fingerprint: str = ""
    system_fingerprint: str = ""
    system_tokens: int = 0
    tools_tokens: int = 0
    history_tokens: int = 0
    logical_prompt_tokens: int = 0
    cached_prefix_tokens: int = 0
    delta_prompt_tokens: int = 0


class ConversationContextBuilder:
    """Converts stateless OpenAI message arrays (including tool loops) into stateful web prompts."""

    def __init__(self, default_mode: str = "smart", auto_reset_new_chat: bool = True) -> None:
        self.default_mode = default_mode
        self.auto_reset_new_chat = auto_reset_new_chat
        # Keyed by browser client/tab identifier -> list of (role, canonical_text) already in web chat
        self._session_turns: Dict[str, List[Tuple[str, str]]] = {}
        self._session_models: Dict[str, Optional[str]] = {}
        self._session_tools_fp: Dict[str, str] = {}
        self._session_system_fp: Dict[str, str] = {}
        self._session_metrics: Dict[str, Dict[str, Any]] = {}
        self._session_compactions: Dict[str, int] = {}

    def clear_session(self, session_key: str = "default") -> None:
        self._session_turns.pop(session_key, None)
        self._session_models.pop(session_key, None)
        self._session_tools_fp.pop(session_key, None)
        self._session_system_fp.pop(session_key, None)
        self._session_metrics.pop(session_key, None)

    def drop_slot(self, session_key: str) -> None:
        self.clear_session(session_key)
        self._session_compactions.pop(session_key, None)

    def migrate_slot_key(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        if old_key in self._session_turns:
            self._session_turns[new_key] = self._session_turns.pop(old_key)
        if old_key in self._session_models:
            self._session_models[new_key] = self._session_models.pop(old_key)
        if old_key in self._session_tools_fp:
            self._session_tools_fp[new_key] = self._session_tools_fp.pop(old_key)
        if old_key in self._session_system_fp:
            self._session_system_fp[new_key] = self._session_system_fp.pop(old_key)
        if old_key in self._session_metrics:
            self._session_metrics[new_key] = self._session_metrics.pop(old_key)
        if old_key in self._session_compactions:
            self._session_compactions[new_key] = self._session_compactions.pop(old_key)

    def get_session_model(self, session_key: str = "default") -> Optional[str]:
        return self._session_models.get(session_key)

    def has_active_conversation(self, session_key: str) -> bool:
        return bool(self._session_turns.get(session_key))

    def has_active_tool_session(self, session_key: str) -> bool:
        """Return True if `session_key` holds an active multi-turn tool-enabled conversation."""
        return bool(self._session_tools_fp.get(session_key)) and len(
            self._session_turns.get(session_key) or []
        ) >= 2

    @staticmethod
    def _system_prompts_compatible(
        prev_sys_fp: Optional[str],
        cur_sys_text: str,
        has_tracked_dialogue: bool = False,
    ) -> Tuple[bool, str]:
        """Return (compatible, appended_system_delta) tolerating volatile tags or appended skill blocks."""
        if prev_sys_fp is None or prev_sys_fp == cur_sys_text:
            return (True, "")
        c_prev = _canonicalize_for_match("system", prev_sys_fp)
        c_cur = _canonicalize_for_match("system", cur_sys_text)
        if c_prev == c_cur:
            return (True, "")
        if has_tracked_dialogue:
            if cur_sys_text.startswith(prev_sys_fp):
                return (True, cur_sys_text[len(prev_sys_fp) :].strip())
            # If the system prompt shares the primary prefix or only mutated dynamic sections, keep session alive
            common_prefix_len = 0
            for ch1, ch2 in zip(c_prev, c_cur):
                if ch1 != ch2:
                    break
                common_prefix_len += 1
            min_len = min(len(c_prev), len(c_cur))
            if min_len > 0 and common_prefix_len >= int(min_len * 0.65):
                prev_paras = {p.strip() for p in prev_sys_fp.split("\n\n") if p.strip()}
                new_paras = [
                    p.strip()
                    for p in cur_sys_text.split("\n\n")
                    if p.strip() and p.strip() not in prev_paras
                ]
                delta_sys = "\n\n".join(new_paras).strip()
                return (True, f"[Updated System Instructions]\n{delta_sys}" if delta_sys else "")
        return (False, "")

    @staticmethod
    def _normalize_messages(messages: Sequence[ChatMessage]) -> Tuple[str, List[Tuple[str, str]]]:
        system_parts: List[str] = []
        dialogue: List[Tuple[str, str]] = []
        call_id_to_name: Dict[str, str] = {}

        for msg in messages:
            role = (msg.role or "user").strip().lower()
            text = msg.text_content().strip()

            if role in {"system", "developer"}:
                if text:
                    system_parts.append(text)
                continue

            if role == "assistant":
                tcs = msg.normalized_tool_calls()
                for tc in tcs:
                    tc_id = str(tc.get("id") or "")
                    fn_name = str((tc.get("function") or {}).get("name") or "")
                    if tc_id and fn_name:
                        call_id_to_name[tc_id] = fn_name
                if tcs:
                    canon_asst = format_assistant_history_entry(text, tcs)
                    dialogue.append(("assistant", canon_asst))
                elif text:
                    dialogue.append(("assistant", text))
                continue

            if role in {"tool", "function"}:
                resolved_name = msg.name or (
                    call_id_to_name.get(msg.tool_call_id) if msg.tool_call_id else None
                )
                formatted_tool = format_tool_result_entry(
                    text,
                    tool_call_id=msg.tool_call_id,
                    name=resolved_name,
                )
                dialogue.append(("tool", formatted_tool))
                continue

            if text:
                dialogue.append(("user", text))

        return ("\n\n".join(system_parts).strip(), dialogue)

    def find_matching_slots(
        self,
        candidate_slot_keys: Sequence[str],
        messages: Sequence[ChatMessage],
        model: str = "gemini-enterprise-web",
    ) -> List[str]:
        """Find worker slot keys whose active browser conversation matches `messages`'s prefix."""
        if not candidate_slot_keys or model_requests_new_chat(model):
            return []

        user_system_text, dialogue = self._normalize_messages(messages)
        if len(dialogue) <= 1:
            return []
        if dialogue[-1][0] == "user" and dialogue[-1][1].startswith("[NEW_CHAT]"):
            return []

        matched: List[Tuple[int, str]] = []
        for slot_key in candidate_slot_keys:
            tracked = self._session_turns.get(slot_key)
            if not tracked or len(tracked) >= len(dialogue):
                continue
            prev_sys_fp = self._session_system_fp.get(slot_key)
            sys_ok, _ = self._system_prompts_compatible(
                prev_sys_fp, user_system_text, has_tracked_dialogue=len(tracked) >= 2
            )
            if not sys_ok:
                continue
            if self._history_matches(tracked, dialogue[: len(tracked)]):
                matched.append((len(tracked), slot_key))

        matched.sort(key=lambda item: item[0], reverse=True)
        return [slot_key for _, slot_key in matched]

    def record_completed_turn(
        self,
        session_key: str,
        sent_history: List[Tuple[str, str]],
        assistant_reply: Optional[str] = "",
        assistant_tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
        active_model: Optional[str] = None,
        tools_fingerprint: Optional[str] = None,
        system_fingerprint: Optional[str] = None,
        system_tokens: Optional[int] = None,
        tools_tokens: Optional[int] = None,
        last_delta_tokens: Optional[int] = None,
        last_strategy: Optional[str] = None,
    ) -> None:
        updated = list(sent_history)
        if assistant_tool_calls:
            canon = format_assistant_history_entry(assistant_reply or "", assistant_tool_calls)
        else:
            canon = (assistant_reply or "").strip()
        updated.append(("assistant", canon))
        self._session_turns[session_key] = updated
        if active_model is not None:
            self._session_models[session_key] = active_model
        if tools_fingerprint is not None:
            self._session_tools_fp[session_key] = tools_fingerprint
        if system_fingerprint is not None:
            self._session_system_fp[session_key] = system_fingerprint

        prev_meta = self._session_metrics.get(session_key) or {}
        eff_sys_tokens = (
            int(system_tokens)
            if system_tokens is not None
            else int(prev_meta.get("system_tokens") or estimate_tokens(self._session_system_fp.get(session_key) or ""))
        )
        eff_tools_tokens = (
            int(tools_tokens)
            if tools_tokens is not None
            else int(prev_meta.get("tools_tokens") or 0)
        )
        hist_tokens = sum(estimate_tokens(txt) for _, txt in updated)
        comp_tokens = estimate_tokens(canon)
        total_ctx = eff_sys_tokens + eff_tools_tokens + hist_tokens
        eff_model = active_model or self._session_models.get(session_key)
        ctx_limit = get_context_window_for_model(eff_model)
        util_pct = round(min(100.0, (total_ctx / max(ctx_limit, 1)) * 100.0), 2)
        self._session_metrics[session_key] = {
            "slot_key": session_key,
            "turn_count": len(updated),
            "user_turns": sum(1 for r, _ in updated if r == "user"),
            "system_tokens": eff_sys_tokens,
            "tools_tokens": eff_tools_tokens,
            "history_tokens": hist_tokens,
            "total_context_tokens": total_ctx,
            "last_delta_tokens": int(last_delta_tokens if last_delta_tokens is not None else prev_meta.get("last_delta_tokens") or 0),
            "last_completion_tokens": comp_tokens,
            "context_limit_tokens": ctx_limit,
            "utilization_pct": util_pct,
            "last_strategy": last_strategy or str(prev_meta.get("last_strategy") or "smart_single_turn"),
            "compaction_count": int(self._session_compactions.get(session_key) or 0),
            "active_model": eff_model or "",
            "updated_at": int(time.time()),
        }

    def build(
        self,
        messages: Sequence[ChatMessage],
        model: str = "gemini-enterprise-web",
        session_key: str = "default",
        mode_override: str | None = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> PreparedPrompt:
        mode = (mode_override or self.default_mode or "smart").lower()
        explicit_new_chat = model_requests_new_chat(model)
        target_model = resolve_target_web_model(model)

        norm_tools = list(tools or [])
        tools_fp = (
            compute_tools_fingerprint(norm_tools)
            if is_tool_generation_enabled(norm_tools, tool_choice)
            else ""
        )

        user_system_text, dialogue = self._normalize_messages(messages)

        tool_sys_text = build_tool_system_instructions(
            norm_tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        fmt_sys_text = build_response_format_instructions(response_format)

        combined_sys_sections = [s for s in (user_system_text, tool_sys_text, fmt_sys_text) if s]
        combined_system_text = "\n\n".join(combined_sys_sections).strip()
        sys_tokens = estimate_tokens("\n\n".join(s for s in (user_system_text, fmt_sys_text) if s))
        tools_tokens = estimate_tokens(tool_sys_text)
        dialogue_tokens = sum(estimate_tokens(txt) for _, txt in dialogue)
        logical_prompt_tokens = sys_tokens + tools_tokens + dialogue_tokens
        prev_tracked_before = list(self._session_turns.get(session_key) or [])

        def _finalize_prepared(
            prompt_str: str,
            reset_flag: bool,
            strategy: str,
            norm_hist: List[Tuple[str, str]],
            cached_prefix: int = 0,
        ) -> PreparedPrompt:
            delta_toks = estimate_tokens(prompt_str)
            eff_hist_toks = sum(estimate_tokens(t) for _, t in norm_hist)
            eff_logical = max(sys_tokens + tools_tokens + eff_hist_toks, 1)
            eff_cached = min(eff_logical, max(0, cached_prefix))
            eff_model = target_model or self._session_models.get(session_key)
            ctx_limit = get_context_window_for_model(eff_model)
            total_ctx = sys_tokens + tools_tokens + eff_hist_toks
            util_pct = round(min(100.0, (total_ctx / max(ctx_limit, 1)) * 100.0), 2)
            self._session_metrics[session_key] = {
                "slot_key": session_key,
                "turn_count": len(norm_hist),
                "user_turns": sum(1 for r, _ in norm_hist if r == "user"),
                "system_tokens": sys_tokens,
                "tools_tokens": tools_tokens,
                "history_tokens": eff_hist_toks,
                "total_context_tokens": total_ctx,
                "last_delta_tokens": delta_toks,
                "last_completion_tokens": int(
                    (self._session_metrics.get(session_key) or {}).get("last_completion_tokens") or 0
                ),
                "context_limit_tokens": ctx_limit,
                "utilization_pct": util_pct,
                "last_strategy": strategy,
                "compaction_count": int(self._session_compactions.get(session_key) or 0),
                "active_model": eff_model or "",
                "updated_at": int(time.time()),
            }
            return PreparedPrompt(
                prompt=prompt_str,
                reset_chat=reset_flag,
                strategy_used=strategy,
                normalized_history=norm_hist,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                system_tokens=sys_tokens,
                tools_tokens=tools_tokens,
                history_tokens=eff_hist_toks,
                logical_prompt_tokens=eff_logical,
                cached_prefix_tokens=eff_cached,
                delta_prompt_tokens=delta_toks,
            )

        if not dialogue:
            fallback_prompt = combined_system_text or "Hello"
            reset_only_sys = explicit_new_chat or self.auto_reset_new_chat
            if reset_only_sys:
                self.clear_session(session_key)
            return _finalize_prepared(
                prompt_str=fallback_prompt,
                reset_flag=reset_only_sys,
                strategy="system_only",
                norm_hist=[("user", fallback_prompt)],
            )

        # Check inline [NEW_CHAT] directive on trailing user turn
        if dialogue[-1][0] == "user" and dialogue[-1][1].startswith("[NEW_CHAT]"):
            explicit_new_chat = True
            stripped_user = dialogue[-1][1][len("[NEW_CHAT]") :].lstrip()
            dialogue[-1] = ("user", stripped_user)

        if explicit_new_chat:
            self.clear_session(session_key)

        has_trailing_tool_results = any(
            r == "tool" for r, _ in self._trailing_non_assistant_slice(dialogue)
        )
        delta_reminder = build_tool_delta_reminder(
            norm_tools,
            tool_choice=tool_choice,
            has_tool_results=has_trailing_tool_results,
        )

        # Track target_model transition on this session_key (UI switches model in-place cleanly)
        if target_model is not None:
            self._session_models[session_key] = target_model

        if mode == "last_message":
            tail_prompt = self._format_delta_entries(
                self._trailing_non_assistant_slice(dialogue) or [dialogue[-1]],
                extra_instructions=[fmt_sys_text, delta_reminder] if len(dialogue) > 1 else [],
            )
            reset_last = explicit_new_chat or (self.auto_reset_new_chat and len(dialogue) == 1)
            if reset_last:
                self.clear_session(session_key)
            prompt = self._with_system_if_first_turn(
                last_text=tail_prompt,
                system_text=combined_system_text,
                is_first_turn=len(dialogue) == 1,
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_last,
                strategy="last_message",
                norm_hist=dialogue,
            )

        if mode == "transcript":
            reset_trans = explicit_new_chat or self.auto_reset_new_chat
            if reset_trans:
                self.clear_session(session_key)
            prompt = self._format_full_transcript(
                system_text=combined_system_text,
                dialogue=dialogue,
                trailing_reminder=delta_reminder,
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_trans,
                strategy="transcript",
                norm_hist=dialogue,
            )

        # Smart mode:
        # 1) Single-turn user prompt -> brand-new local conversation!
        #    When `auto_reset_new_chat` is True (default), ALWAYS reset_chat=True (even on the first
        #    request after server startup, in case the browser tab already has an old `/r/session/...` open;
        #    page-driver.js fast-paths in 0ms if the tab is already on a blank new chat).
        if len(dialogue) == 1 and dialogue[0][0] == "user":
            reset_chat = explicit_new_chat or self.auto_reset_new_chat
            if reset_chat:
                self.clear_session(session_key)
                if target_model is not None:
                    self._session_models[session_key] = target_model
            prompt = self._with_system_if_first_turn(
                last_text=dialogue[0][1],
                system_text=combined_system_text,
                is_first_turn=True,
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_chat,
                strategy="smart_single_turn",
                norm_hist=dialogue,
            )

        # 2) Multi-turn continuation of the SAME local conversation (including tool_result loops!)
        tracked = self._session_turns.get(session_key)
        prev_sys_fp = self._session_system_fp.get(session_key)
        system_matches, sys_delta_block = self._system_prompts_compatible(
            prev_sys_fp, user_system_text, has_tracked_dialogue=bool(tracked and len(tracked) >= 2)
        )
        if (
            not explicit_new_chat
            and tracked
            and system_matches
            and len(tracked) < len(dialogue)
            and self._history_matches(tracked, dialogue[: len(tracked)])
        ):
            delta_entries = dialogue[len(tracked) :]
            prev_fp = self._session_tools_fp.get(session_key, "")
            extra_blocks: List[str] = []
            if sys_delta_block:
                extra_blocks.append(sys_delta_block)
            if tools_fp and tools_fp != prev_fp and tool_sys_text:
                extra_blocks.append(tool_sys_text)
            if fmt_sys_text:
                extra_blocks.append(fmt_sys_text)

            delta_has_tools = any(r == "tool" for r, _ in delta_entries)
            inc_reminder = build_tool_delta_reminder(
                norm_tools,
                tool_choice=tool_choice,
                has_tool_results=delta_has_tools,
            )
            if inc_reminder:
                extra_blocks.append(inc_reminder)

            cached_prefix = (
                sys_tokens
                + (tools_tokens if (not tools_fp or tools_fp == prev_fp) else 0)
                + sum(estimate_tokens(t) for _, t in tracked)
            )
            prompt = self._format_delta_entries(delta_entries, extra_instructions=extra_blocks)
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=False,
                strategy="smart_incremental_delta",
                norm_hist=dialogue,
                cached_prefix=cached_prefix,
            )

        # 3) Switched to a DIFFERENT multi-turn local conversation not matching current browser tab:
        #    Start a clean web session (`reset_chat=True` when auto_reset_new_chat=True) and pack its history.
        if prev_tracked_before and not explicit_new_chat:
            self._session_compactions[session_key] = (
                int(self._session_compactions.get(session_key) or 0) + 1
            )
        reset_packed = explicit_new_chat or self.auto_reset_new_chat
        if reset_packed:
            self.clear_session(session_key)
            if target_model is not None:
                self._session_models[session_key] = target_model

        prompt = self._format_full_transcript(
            system_text=combined_system_text,
            dialogue=dialogue,
            trailing_reminder=delta_reminder,
        )
        return _finalize_prepared(
            prompt_str=prompt,
            reset_flag=reset_packed,
            strategy="smart_packed_context",
            norm_hist=dialogue,
        )

    @staticmethod
    def _trailing_non_assistant_slice(dialogue: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
        idx = len(dialogue)
        while idx > 0 and dialogue[idx - 1][0] != "assistant":
            idx -= 1
        return list(dialogue[idx:])

    @staticmethod
    def _format_delta_entries(
        delta_entries: Sequence[Tuple[str, str]],
        extra_instructions: Optional[Sequence[str]] = None,
    ) -> str:
        extras = [e.strip() for e in (extra_instructions or []) if e and e.strip()]
        if len(delta_entries) == 1 and delta_entries[0][0] == "user" and not extras:
            return delta_entries[0][1]

        blocks: List[str] = []
        for role, content in delta_entries:
            if role == "tool":
                blocks.append(content)
            elif role == "assistant":
                blocks.append(f"Assistant: {content}")
            else:
                blocks.append(content if len(delta_entries) == 1 else f"User: {content}")

        if delta_entries and delta_entries[-1][0] == "assistant":
            blocks.append("Please continue based on the conversation above.")

        blocks.extend(extras)
        return "\n\n".join(b for b in blocks if b)

    @classmethod
    def _history_matches(
        cls,
        tracked: Sequence[Tuple[str, str]],
        incoming_prefix: Sequence[Tuple[str, str]],
    ) -> bool:
        if len(tracked) != len(incoming_prefix):
            return False
        for (r1, t1), (r2, t2) in zip(tracked, incoming_prefix):
            if r1 != r2:
                return False
            s1 = t1.strip()
            s2 = t2.strip()
            if s1 == s2:
                continue
            if _canonicalize_for_match(r1, s1) == _canonicalize_for_match(r2, s2):
                continue
            return False
        return True

    @staticmethod
    def _with_system_if_first_turn(last_text: str, system_text: str, is_first_turn: bool) -> str:
        if system_text and is_first_turn:
            return f"[System Instructions]\n{system_text}\n\n[User Request]\n{last_text}"
        return last_text

    @classmethod
    def _format_full_transcript(
        cls,
        system_text: str,
        dialogue: Sequence[Tuple[str, str]],
        trailing_reminder: str = "",
    ) -> str:
        sections: List[str] = []
        if system_text:
            sections.append(f"[System Instructions]\n{system_text}")

        tail = cls._trailing_non_assistant_slice(dialogue)
        split_idx = len(dialogue) - len(tail) if tail else max(0, len(dialogue) - 1)
        prior = dialogue[:split_idx]
        current_entries = dialogue[split_idx:]

        if prior:
            lines: List[str] = ["[Conversation History]"]
            for role, content in prior:
                if role == "user":
                    lines.append(f"User: {content}")
                elif role == "assistant":
                    lines.append(f"Assistant: {content}")
                else:
                    lines.append(content)
            sections.append("\n".join(lines))

        current_body = cls._format_delta_entries(
            current_entries,
            extra_instructions=[trailing_reminder] if trailing_reminder else None,
        )
        heading = (
            "[Tool Execution Results]"
            if current_entries and all(r == "tool" for r, _ in current_entries)
            else "[Current User Request]"
        )
        sections.append(f"{heading}\n{current_body}")
        return "\n\n".join(sections)

    def get_slot_context_metrics(self, session_key: str) -> Optional[Dict[str, Any]]:
        meta = self._session_metrics.get(session_key)
        return dict(meta) if meta else None

    def get_context_snapshot(
        self,
        worker_slots: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Return live per-tab context occupancy and primary active session metrics."""
        slot_info_map: Dict[str, Dict[str, Any]] = {}
        for item in worker_slots or []:
            if isinstance(item, dict) and item.get("slot_key"):
                slot_info_map[str(item["slot_key"])] = item

        all_keys: List[str] = list(slot_info_map.keys())
        for k in self._session_metrics.keys():
            if k not in slot_info_map:
                all_keys.append(k)

        tabs_out: List[Dict[str, Any]] = []
        for skey in all_keys:
            s_info = slot_info_map.get(skey) or {}
            meta = self._session_metrics.get(skey) or {}
            browser_model = str(s_info.get("active_model") or meta.get("active_model") or "")
            ctx_limit = int(
                meta.get("context_limit_tokens")
                or get_context_window_for_model(meta.get("active_model"), browser_model)
                or DEFAULT_CONTEXT_WINDOW
            )
            total_ctx = int(meta.get("total_context_tokens") or 0)
            util_pct = round(min(100.0, (total_ctx / max(ctx_limit, 1)) * 100.0), 2)
            tabs_out.append(
                {
                    "slot_key": skey,
                    "tab_id": s_info.get("tab_id"),
                    "tab_title": s_info.get("tab_title") or "",
                    "busy": bool(s_info.get("busy", False)),
                    "pinned_session_id": s_info.get("pinned_session_id"),
                    "active_model": browser_model or str(self._session_models.get(skey) or "Auto"),
                    "turn_count": int(meta.get("turn_count") or len(self._session_turns.get(skey) or [])),
                    "user_turns": int(meta.get("user_turns") or 0),
                    "system_tokens": int(meta.get("system_tokens") or 0),
                    "tools_tokens": int(meta.get("tools_tokens") or 0),
                    "history_tokens": int(meta.get("history_tokens") or 0),
                    "total_context_tokens": total_ctx,
                    "last_delta_tokens": int(meta.get("last_delta_tokens") or 0),
                    "last_completion_tokens": int(meta.get("last_completion_tokens") or 0),
                    "context_limit_tokens": ctx_limit,
                    "utilization_pct": util_pct,
                    "last_strategy": str(meta.get("last_strategy") or "idle"),
                    "compaction_count": int(self._session_compactions.get(skey) or 0),
                    "updated_at": int(meta.get("updated_at") or s_info.get("last_used_at") or 0),
                }
            )

        tabs_out.sort(
            key=lambda x: (
                1 if x["busy"] else 0,
                x["updated_at"],
                x["total_context_tokens"],
            ),
            reverse=True,
        )
        primary = tabs_out[0] if tabs_out else {
            "slot_key": "",
            "tab_id": None,
            "busy": False,
            "active_model": "Auto",
            "turn_count": 0,
            "system_tokens": 0,
            "tools_tokens": 0,
            "history_tokens": 0,
            "total_context_tokens": 0,
            "last_delta_tokens": 0,
            "last_completion_tokens": 0,
            "context_limit_tokens": DEFAULT_CONTEXT_WINDOW,
            "utilization_pct": 0.0,
            "last_strategy": "idle",
            "compaction_count": 0,
            "updated_at": 0,
        }
        return {
            "default_context_limit": DEFAULT_CONTEXT_WINDOW,
            "active_sessions": sum(1 for t in tabs_out if t["turn_count"] > 0),
            "total_compactions": sum(int(t["compaction_count"]) for t in tabs_out),
            "primary": primary,
            "slots": tabs_out,
        }

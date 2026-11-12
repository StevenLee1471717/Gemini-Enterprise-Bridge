"""Multi-turn prompt construction, model tracking, and agentic conversation delta tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import time

from multimodal import NormalizedAttachment, deduplicate_attachments, estimate_attachment_tokens
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
class _DialogueEntry:
    role: str
    display_text: str
    match_text: str
    history_text: str
    attachments: List[NormalizedAttachment] = field(default_factory=list)


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
    attachments: List[NormalizedAttachment] = field(default_factory=list)


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
    def _normalize_message_entries(
        messages: Sequence[ChatMessage],
    ) -> Tuple[str, List[_DialogueEntry], List[NormalizedAttachment]]:
        system_parts: List[str] = []
        system_attachments: List[NormalizedAttachment] = []
        entries: List[_DialogueEntry] = []
        call_id_to_name: Dict[str, str] = {}

        for msg in messages:
            role = (msg.role or "user").strip().lower()
            text = msg.text_content().strip()
            atts = msg.extract_attachments()

            if role in {"system", "developer"}:
                if text:
                    system_parts.append(text)
                for att in atts:
                    extracted_sys = att.extract_text_fallback()
                    if extracted_sys:
                        system_parts.append(
                            f"[System Reference File: {att.filename}]\n{extracted_sys}"
                        )
                    else:
                        system_attachments.append(att)
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
                    entries.append(
                        _DialogueEntry(
                            role="assistant",
                            display_text=canon_asst,
                            match_text=canon_asst,
                            history_text=canon_asst,
                        )
                    )
                elif text:
                    entries.append(
                        _DialogueEntry(
                            role="assistant",
                            display_text=text,
                            match_text=text,
                            history_text=text,
                        )
                    )
                continue

            if role in {"tool", "function"}:
                resolved_name = msg.name or (
                    call_id_to_name.get(msg.tool_call_id) if msg.tool_call_id else None
                )
                eff_tool_text = text or ("[Attached file(s) in tool output]" if atts else "")
                formatted_tool = format_tool_result_entry(
                    eff_tool_text,
                    tool_call_id=msg.tool_call_id,
                    name=resolved_name,
                )
                att_fp = " ".join(a.fingerprint_tag() for a in atts)
                att_notes = "\n".join(a.history_note() for a in atts)
                match_t = f"{formatted_tool}\n{att_fp}".strip() if att_fp else formatted_tool
                hist_t = f"{formatted_tool}\n{att_notes}".strip() if att_notes else formatted_tool
                entries.append(
                    _DialogueEntry(
                        role="tool",
                        display_text=formatted_tool,
                        match_text=match_t,
                        history_text=hist_t,
                        attachments=atts,
                    )
                )
                continue

            if text or atts:
                att_fp = " ".join(a.fingerprint_tag() for a in atts)
                att_notes = "\n".join(a.history_note() for a in atts)
                match_t = f"{text}\n{att_fp}".strip() if att_fp else text
                hist_t = f"{text}\n{att_notes}".strip() if att_notes else text
                entries.append(
                    _DialogueEntry(
                        role="user",
                        display_text=text,
                        match_text=match_t,
                        history_text=hist_t,
                        attachments=atts,
                    )
                )

        return ("\n\n".join(system_parts).strip(), entries, system_attachments)

    @classmethod
    def _normalize_messages(cls, messages: Sequence[ChatMessage]) -> Tuple[str, List[Tuple[str, str]]]:
        sys_text, entries, _ = cls._normalize_message_entries(messages)
        return (sys_text, [(e.role, e.match_text) for e in entries])

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

        user_system_text, entries, system_attachments = self._normalize_message_entries(messages)
        dialogue = [(e.role, e.match_text) for e in entries]
        all_dialogue_atts = deduplicate_attachments(
            [*system_attachments, *(a for e in entries for a in e.attachments)]
        )
        all_att_tokens = sum(estimate_attachment_tokens(a) for a in all_dialogue_atts)

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
        prev_tracked_before = list(self._session_turns.get(session_key) or [])

        def _finalize_prepared(
            prompt_str: str,
            reset_flag: bool,
            strategy: str,
            norm_hist: List[Tuple[str, str]],
            cached_prefix: int = 0,
            selected_attachments: Optional[List[NormalizedAttachment]] = None,
        ) -> PreparedPrompt:
            eff_atts = list(selected_attachments or [])
            delta_att_toks = sum(estimate_attachment_tokens(a) for a in eff_atts)
            delta_toks = estimate_tokens(prompt_str) + delta_att_toks
            eff_hist_toks = sum(estimate_tokens(t) for _, t in norm_hist) + all_att_tokens
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
                attachments=eff_atts,
            )

        if not entries:
            fallback_prompt = combined_system_text or ("" if system_attachments else "Hello")
            reset_only_sys = explicit_new_chat or self.auto_reset_new_chat
            if reset_only_sys:
                self.clear_session(session_key)
            return _finalize_prepared(
                prompt_str=fallback_prompt,
                reset_flag=reset_only_sys,
                strategy="system_only",
                norm_hist=[("user", fallback_prompt or "[System Attachments]")],
                selected_attachments=deduplicate_attachments(system_attachments),
            )

        # Check inline [NEW_CHAT] directive on trailing user turn
        if entries[-1].role == "user" and (
            entries[-1].display_text.startswith("[NEW_CHAT]")
            or entries[-1].match_text.startswith("[NEW_CHAT]")
        ):
            explicit_new_chat = True
            stripped_user = (
                entries[-1].display_text[len("[NEW_CHAT]") :].lstrip()
                if entries[-1].display_text.startswith("[NEW_CHAT]")
                else entries[-1].display_text
            )
            att_fp = " ".join(a.fingerprint_tag() for a in entries[-1].attachments)
            att_notes = "\n".join(a.history_note() for a in entries[-1].attachments)
            entries[-1] = _DialogueEntry(
                role="user",
                display_text=stripped_user,
                match_text=f"{stripped_user}\n{att_fp}".strip() if att_fp else stripped_user,
                history_text=f"{stripped_user}\n{att_notes}".strip() if att_notes else stripped_user,
                attachments=entries[-1].attachments,
            )
            dialogue = [(e.role, e.match_text) for e in entries]

        if explicit_new_chat:
            self.clear_session(session_key)

        tail_entries = self._trailing_non_assistant_entries(entries)
        has_trailing_tool_results = any(e.role == "tool" for e in tail_entries)
        delta_reminder = build_tool_delta_reminder(
            norm_tools,
            tool_choice=tool_choice,
            has_tool_results=has_trailing_tool_results,
        )

        # Track target_model transition on this session_key (UI switches model in-place cleanly)
        if target_model is not None:
            self._session_models[session_key] = target_model

        if mode == "last_message":
            chosen_tail = tail_entries or [entries[-1]]
            tail_atts = deduplicate_attachments(
                [
                    *(system_attachments if len(entries) == 1 else []),
                    *(a for e in chosen_tail for a in e.attachments),
                ]
            )
            tail_prompt = self._format_delta_entries(
                [(e.role, e.display_text) for e in chosen_tail],
                extra_instructions=[fmt_sys_text, delta_reminder] if len(entries) > 1 else [],
                has_attachments=bool(tail_atts),
            )
            reset_last = explicit_new_chat or (self.auto_reset_new_chat and len(entries) == 1)
            if reset_last:
                self.clear_session(session_key)
            prompt = self._with_system_if_first_turn(
                last_text=tail_prompt,
                system_text=combined_system_text,
                is_first_turn=len(entries) == 1,
                has_attachments=bool(tail_atts),
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_last,
                strategy="last_message",
                norm_hist=dialogue,
                selected_attachments=tail_atts,
            )

        if mode == "transcript":
            reset_trans = explicit_new_chat or self.auto_reset_new_chat
            if reset_trans:
                self.clear_session(session_key)
            prompt = self._format_full_transcript_entries(
                system_text=combined_system_text,
                entries=entries,
                trailing_reminder=delta_reminder,
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_trans,
                strategy="transcript",
                norm_hist=dialogue,
                selected_attachments=all_dialogue_atts,
            )

        # Smart mode:
        # 1) Single-turn user prompt -> brand-new local conversation!
        if len(entries) == 1 and entries[0].role == "user":
            reset_chat = explicit_new_chat or self.auto_reset_new_chat
            if reset_chat:
                self.clear_session(session_key)
                if target_model is not None:
                    self._session_models[session_key] = target_model
            single_atts = deduplicate_attachments([*system_attachments, *entries[0].attachments])
            prompt = self._with_system_if_first_turn(
                last_text=entries[0].display_text,
                system_text=combined_system_text,
                is_first_turn=True,
                has_attachments=bool(single_atts),
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=reset_chat,
                strategy="smart_single_turn",
                norm_hist=dialogue,
                selected_attachments=single_atts,
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
            delta_slice = entries[len(tracked) :]
            delta_atts = deduplicate_attachments(a for e in delta_slice for a in e.attachments)
            prev_fp = self._session_tools_fp.get(session_key, "")
            extra_blocks: List[str] = []
            if sys_delta_block:
                extra_blocks.append(sys_delta_block)
            if tools_fp and tools_fp != prev_fp and tool_sys_text:
                extra_blocks.append(tool_sys_text)
            if fmt_sys_text:
                extra_blocks.append(fmt_sys_text)
            delta_has_tools = any(e.role == "tool" for e in delta_slice)
            inc_reminder = build_tool_delta_reminder(
                norm_tools,
                tool_choice=tool_choice,
                has_tool_results=delta_has_tools,
            )
            if inc_reminder:
                extra_blocks.append(inc_reminder)

            prior_att_tokens = sum(
                estimate_attachment_tokens(a)
                for e in entries[: len(tracked)]
                for a in e.attachments
            ) + sum(estimate_attachment_tokens(a) for a in system_attachments)
            cached_prefix = (
                sys_tokens
                + (tools_tokens if (not tools_fp or tools_fp == prev_fp) else 0)
                + sum(estimate_tokens(t) for _, t in tracked)
                + prior_att_tokens
            )
            prompt = self._format_delta_entries(
                [
                    (
                        e.role,
                        e.history_text if (len(delta_slice) > 1 and e.attachments) else e.display_text,
                    )
                    for e in delta_slice
                ],
                extra_instructions=extra_blocks,
                has_attachments=bool(delta_atts),
            )
            return _finalize_prepared(
                prompt_str=prompt,
                reset_flag=False,
                strategy="smart_incremental_delta",
                norm_hist=dialogue,
                cached_prefix=cached_prefix,
                selected_attachments=delta_atts,
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
        prompt = self._format_full_transcript_entries(
            system_text=combined_system_text,
            entries=entries,
            trailing_reminder=delta_reminder,
        )
        return _finalize_prepared(
            prompt_str=prompt,
            reset_flag=reset_packed,
            strategy="smart_packed_context",
            norm_hist=dialogue,
            selected_attachments=all_dialogue_atts,
        )

    @staticmethod
    def _trailing_non_assistant_entries(entries: Sequence[_DialogueEntry]) -> List[_DialogueEntry]:
        idx = len(entries)
        while idx > 0 and entries[idx - 1].role != "assistant":
            idx -= 1
        return list(entries[idx:])

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
        has_attachments: bool = False,
    ) -> str:
        extras = [e.strip() for e in (extra_instructions or []) if e and e.strip()]
        if len(delta_entries) == 1 and delta_entries[0][0] == "user" and not extras:
            return delta_entries[0][1]

        blocks: List[str] = []
        for role, content in delta_entries:
            eff_content = content or ("[Please process the attached file(s).]" if has_attachments else "")
            if role == "tool":
                blocks.append(eff_content)
            elif role == "assistant":
                blocks.append(f"Assistant: {eff_content}")
            else:
                if len(delta_entries) == 1:
                    if eff_content:
                        blocks.append(eff_content)
                else:
                    blocks.append(f"User: {eff_content}")
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
    def _with_system_if_first_turn(
        last_text: str,
        system_text: str,
        is_first_turn: bool,
        has_attachments: bool = False,
    ) -> str:
        if system_text and is_first_turn:
            eff_last = last_text or ("[Please process the attached file(s).]" if has_attachments else "")
            return f"[System Instructions]\n{system_text}\n\n[User Request]\n{eff_last}"
        return last_text

    @classmethod
    def _format_full_transcript_entries(
        cls,
        system_text: str,
        entries: Sequence[_DialogueEntry],
        trailing_reminder: str = "",
    ) -> str:
        sections: List[str] = []
        if system_text:
            sections.append(f"[System Instructions]\n{system_text}")

        tail = cls._trailing_non_assistant_entries(entries)
        split_idx = len(entries) - len(tail) if tail else max(0, len(entries) - 1)
        prior = entries[:split_idx]
        current_entries = entries[split_idx:]

        if prior:
            lines: List[str] = ["[Conversation History]"]
            for entry in prior:
                if entry.role == "user":
                    lines.append(f"User: {entry.history_text}")
                elif entry.role == "assistant":
                    lines.append(f"Assistant: {entry.history_text}")
                else:
                    lines.append(entry.history_text)
            sections.append("\n".join(lines))

        has_cur_atts = any(bool(e.attachments) for e in current_entries)
        current_tuples = [
            (
                e.role,
                e.history_text if (len(current_entries) > 1 and e.attachments) else e.display_text,
            )
            for e in current_entries
        ]
        current_body = cls._format_delta_entries(
            current_tuples,
            extra_instructions=[trailing_reminder] if trailing_reminder else None,
            has_attachments=has_cur_atts,
        )
        if not current_body and has_cur_atts:
            current_body = "[Please process the attached file(s).]"
        heading = (
            "[Tool Execution Results]"
            if current_entries and all(e.role == "tool" for e in current_entries)
            else "[Current User Request]"
        )
        sections.append(f"{heading}\n{current_body}")
        return "\n\n".join(sections)

    @classmethod
    def _format_full_transcript(
        cls,
        system_text: str,
        dialogue: Sequence[Tuple[str, str]],
        trailing_reminder: str = "",
    ) -> str:
        entries = [
            _DialogueEntry(role=r, display_text=t, match_text=t, history_text=t)
            for r, t in dialogue
        ]
        return cls._format_full_transcript_entries(
            system_text=system_text,
            entries=entries,
            trailing_reminder=trailing_reminder,
        )

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

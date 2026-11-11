"""Multi-turn prompt construction, model tracking, and agentic conversation delta tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from schemas import ChatMessage, model_requests_new_chat, resolve_target_web_model
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


@dataclass
class PreparedPrompt:
    prompt: str
    reset_chat: bool
    strategy_used: str
    normalized_history: List[Tuple[str, str]]
    target_model: Optional[str] = None
    tools_fingerprint: str = ""
    system_fingerprint: str = ""


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

    def clear_session(self, session_key: str = "default") -> None:
        self._session_turns.pop(session_key, None)
        self._session_models.pop(session_key, None)
        self._session_tools_fp.pop(session_key, None)
        self._session_system_fp.pop(session_key, None)

    def get_session_model(self, session_key: str = "default") -> Optional[str]:
        return self._session_models.get(session_key)

    def record_completed_turn(
        self,
        session_key: str,
        sent_history: List[Tuple[str, str]],
        assistant_reply: Optional[str] = "",
        assistant_tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
        active_model: Optional[str] = None,
        tools_fingerprint: Optional[str] = None,
        system_fingerprint: Optional[str] = None,
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

        tool_sys_text = build_tool_system_instructions(
            norm_tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        fmt_sys_text = build_response_format_instructions(response_format)

        user_system_text = "\n\n".join(system_parts).strip()
        combined_sys_sections = [s for s in (user_system_text, tool_sys_text, fmt_sys_text) if s]
        combined_system_text = "\n\n".join(combined_sys_sections).strip()

        if not dialogue:
            fallback_prompt = combined_system_text or "Hello"
            reset_only_sys = explicit_new_chat or self.auto_reset_new_chat
            if reset_only_sys:
                self.clear_session(session_key)
            return PreparedPrompt(
                prompt=fallback_prompt,
                reset_chat=reset_only_sys,
                strategy_used="system_only",
                normalized_history=[("user", fallback_prompt)],
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
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
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_last,
                strategy_used="last_message",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
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
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_trans,
                strategy_used="transcript",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
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
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_chat,
                strategy_used="smart_single_turn",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
            )

        # 2) Multi-turn continuation of the SAME local conversation (including tool_result loops!)
        tracked = self._session_turns.get(session_key)
        prev_sys_fp = self._session_system_fp.get(session_key)
        system_matches = (prev_sys_fp is None) or (prev_sys_fp == user_system_text)
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

            prompt = self._format_delta_entries(delta_entries, extra_instructions=extra_blocks)
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=False,
                strategy_used="smart_incremental_delta",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
            )

        # 3) Switched to a DIFFERENT multi-turn local conversation not matching current browser tab:
        #    Start a clean web session (`reset_chat=True` when auto_reset_new_chat=True) and pack its history.
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
        return PreparedPrompt(
            prompt=prompt,
            reset_chat=reset_packed,
            strategy_used="smart_packed_context",
            normalized_history=dialogue,
            target_model=target_model,
            tools_fingerprint=tools_fp,
            system_fingerprint=user_system_text,
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
            # Allow matching assistant tool_call turns even if a client rewrote tool_call IDs
            if r1 == "assistant" and "[TOOL_CALL]" in s1 and "[TOOL_CALL]" in s2:
                if _CALL_ID_IN_CANON_RE.sub("", s1) == _CALL_ID_IN_CANON_RE.sub("", s2):
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

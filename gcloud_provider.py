"""Optional Option-1 upstream: Official Google Cloud CLI (ADC) -> Vertex AI / Agentspace API."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import httpx

from config import BridgeSettings
from schemas import ChatMessage
from tool_calling import (
    build_response_format_instructions,
    build_tool_delta_reminder,
    build_tool_system_instructions,
    format_assistant_history_entry,
    format_tool_result_entry,
)


class GCloudUpstreamError(RuntimeError):
    """Raised when official gcloud / Vertex AI upstream is unconfigured or fails."""


class GCloudVertexProvider:
    """Calls Vertex AI Gemini or Discovery Engine (Agentspace) using local gcloud credentials."""

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self._cached_token: str = ""
        self._token_expires_at: float = 0.0

    def is_configured(self) -> bool:
        return bool(self.settings.gcp_project_id)

    async def get_access_token(self) -> str:
        now = time.time()
        if self._cached_token and now < self._token_expires_at:
            return self._cached_token

        gcloud_bin = shutil.which(self.settings.gcloud_binary) or shutil.which("gcloud.cmd")
        if not gcloud_bin:
            raise GCloudUpstreamError(
                "未找到 gcloud CLI，请先安装 Google Cloud SDK 并执行 `gcloud auth application-default login`。"
            )

        token = await asyncio.to_thread(self._run_gcloud_token, gcloud_bin)
        self._cached_token = token
        # Access tokens last ~3600s; refresh conservatively every 45 minutes
        self._token_expires_at = now + 2700.0
        return token

    @staticmethod
    def _run_gcloud_token(gcloud_bin: str) -> str:
        commands = [
            [gcloud_bin, "auth", "application-default", "print-access-token"],
            [gcloud_bin, "auth", "print-access-token"],
        ]
        last_err = ""
        for cmd in commands:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip().splitlines()[-1].strip()
                last_err = (proc.stderr or proc.stdout or "").strip()
            except Exception as exc:
                last_err = str(exc)
        raise GCloudUpstreamError(f"无法从 gcloud 获取访问令牌: {last_err or '未知错误'}")

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        if not self.is_configured():
            raise GCloudUpstreamError(
                "未配置 GCP_PROJECT_ID。若需启用方案一（官方 GCloud 模式），请在 .env 中配置 GCP_PROJECT_ID。"
            )

        token = await self.get_access_token()
        if self.settings.gcp_engine_id:
            async for chunk in self._stream_discovery_engine(
                token,
                messages,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                response_format=response_format,
            ):
                yield chunk
            return

        async for chunk in self._stream_vertex_gemini(
            token,
            messages,
            temperature,
            max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format,
        ):
            yield chunk

    async def complete_chat(
        self,
        messages: Sequence[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        parts: List[str] = []
        async for chunk in self.stream_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format,
        ):
            parts.append(chunk)
        return "".join(parts).strip()

    async def _stream_discovery_engine(
        self,
        token: str,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """Call Vertex AI Search / Agentspace Discovery Engine streamAssist API."""
        loc = self.settings.gcp_location
        host = (
            "discoveryengine.googleapis.com"
            if loc == "global"
            else f"{loc}-discoveryengine.googleapis.com"
        )
        url = (
            f"https://{host}/v1alpha/projects/{self.settings.gcp_project_id}"
            f"/locations/{loc}/collections/{self.settings.gcp_collection_id}"
            f"/engines/{self.settings.gcp_engine_id}/assistants/default_assistant:streamAssist"
        )

        norm_tools = list(tools or [])
        tool_sys = build_tool_system_instructions(
            norm_tools, tool_choice=tool_choice, parallel_tool_calls=parallel_tool_calls
        )
        fmt_sys = build_response_format_instructions(response_format)

        query_blocks: List[str] = []
        if tool_sys:
            query_blocks.append(tool_sys)
        if fmt_sys:
            query_blocks.append(fmt_sys)

        call_names: Dict[str, str] = {}
        for msg in messages:
            role = (msg.role or "user").lower()
            txt = msg.text_content().strip()
            if role in {"system", "developer"} and txt:
                query_blocks.append(f"[System Instructions]\n{txt}")
            elif role == "assistant":
                tcs = msg.normalized_tool_calls()
                for tc in tcs:
                    if tc.get("id") and (tc.get("function") or {}).get("name"):
                        call_names[str(tc["id"])] = str(tc["function"]["name"])
                asst_txt = format_assistant_history_entry(txt, tcs) if tcs else txt
                if asst_txt:
                    query_blocks.append(f"Assistant: {asst_txt}")
            elif role in {"tool", "function"}:
                rname = msg.name or (call_names.get(msg.tool_call_id) if msg.tool_call_id else None)
                query_blocks.append(
                    format_tool_result_entry(txt, tool_call_id=msg.tool_call_id, name=rname)
                )
            elif txt:
                query_blocks.append(f"User: {txt}")

        reminder = build_tool_delta_reminder(
            norm_tools,
            tool_choice=tool_choice,
            has_tool_results=bool(messages and (messages[-1].role or "").lower() in {"tool", "function"}),
        )
        if reminder:
            query_blocks.append(reminder)

        payload = {"query": {"text": "\n\n".join(query_blocks) or "Hello"}}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": self.settings.gcp_project_id,
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise GCloudUpstreamError(
                    f"Discovery Engine streamAssist 返回 HTTP {resp.status_code}: {resp.text[:400]}"
                )
            data = resp.json()
            items = data if isinstance(data, list) else [data]
            for packet in items:
                answer = packet.get("answer", {}) if isinstance(packet, dict) else {}
                for reply in answer.get("replies", []) or []:
                    grounded = reply.get("groundedContent", {})
                    content = grounded.get("content", {})
                    if content.get("thought"):
                        continue
                    text = content.get("text", "")
                    if text:
                        yield text

    async def _stream_vertex_gemini(
        self,
        token: str,
        messages: Sequence[ChatMessage],
        temperature: Optional[float],
        max_tokens: Optional[int],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """Call Vertex AI Gemini streamGenerateContent SSE endpoint."""
        loc = self.settings.gcp_vertex_location
        model_id = self.settings.gcp_vertex_model
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = (
            f"https://{host}/v1/projects/{self.settings.gcp_project_id}"
            f"/locations/{loc}/publishers/google/models/{model_id}:streamGenerateContent?alt=sse"
        )

        norm_tools = list(tools or [])
        system_texts: List[str] = []
        contents: List[Dict[str, Any]] = []
        call_names: Dict[str, str] = {}

        def _append_vertex_turn(vertex_role: str, part_text: str) -> None:
            if not part_text:
                return
            if contents and contents[-1]["role"] == vertex_role:
                contents[-1]["parts"].append({"text": part_text})
            else:
                contents.append({"role": vertex_role, "parts": [{"text": part_text}]})

        for msg in messages:
            role = (msg.role or "user").lower()
            text = msg.text_content().strip()
            if role in {"system", "developer"}:
                if text:
                    system_texts.append(text)
            elif role == "assistant":
                tcs = msg.normalized_tool_calls()
                for tc in tcs:
                    if tc.get("id") and (tc.get("function") or {}).get("name"):
                        call_names[str(tc["id"])] = str(tc["function"]["name"])
                asst_text = format_assistant_history_entry(text, tcs) if tcs else text
                _append_vertex_turn("model", asst_text)
            elif role in {"tool", "function"}:
                rname = msg.name or (call_names.get(msg.tool_call_id) if msg.tool_call_id else None)
                tool_block = format_tool_result_entry(
                    text, tool_call_id=msg.tool_call_id, name=rname
                )
                _append_vertex_turn("user", tool_block)
            else:
                _append_vertex_turn("user", text)

        tool_sys = build_tool_system_instructions(
            norm_tools, tool_choice=tool_choice, parallel_tool_calls=parallel_tool_calls
        )
        fmt_sys = build_response_format_instructions(response_format)
        if tool_sys:
            system_texts.append(tool_sys)
        if fmt_sys:
            system_texts.append(fmt_sys)

        reminder = build_tool_delta_reminder(
            norm_tools,
            tool_choice=tool_choice,
            has_tool_results=bool(messages and (messages[-1].role or "").lower() in {"tool", "function"}),
        )
        if reminder:
            _append_vertex_turn("user", reminder)

        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Hello"}]}]

        body: Dict[str, Any] = {"contents": contents}
        if system_texts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}

        gen_cfg: Dict[str, Any] = {}
        if temperature is not None:
            gen_cfg["temperature"] = temperature
        if max_tokens is not None:
            gen_cfg["maxOutputTokens"] = max_tokens
        if gen_cfg:
            body["generationConfig"] = gen_cfg

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": self.settings.gcp_project_id,
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise GCloudUpstreamError(
                        f"Vertex AI Gemini 返回 HTTP {resp.status_code}: {err_body[:400]}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for cand in chunk.get("candidates", []) or []:
                        parts = (cand.get("content") or {}).get("parts") or []
                        for part in parts:
                            if part.get("thought"):
                                continue
                            txt = part.get("text")
                            if txt:
                                yield txt

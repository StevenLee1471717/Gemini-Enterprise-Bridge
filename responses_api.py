"""Responses protocol used by Codex, translated to the shared chat pipeline."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from media_inputs import extract_part_marker
from schemas import ChatCompletionRequest, ChatMessage
from streaming_utils import with_keepalive


class ResponsesError(ValueError):
    pass


class ResponseStore:
    """Bounded continuation storage; checkpoints survive restarts and expiry."""
    MAX_ENTRY_BYTES = 32 * 1024 * 1024
    MAX_TOTAL_BYTES = 512 * 1024 * 1024

    def __init__(self, file_store: Any = None, directory=None, ttl_seconds=86400) -> None:
        self.file_store = file_store
        self.ttl_seconds = ttl_seconds
        self.lock = RLock()
        database = ":memory:"
        if directory:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            database = directory / "responses.sqlite3"
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        if database != ":memory:":
            database.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, created REAL, checkpoint INTEGER, payload BLOB)")
        self.db.commit()

    def put(self, key: str, response: dict, history: list) -> None:
        payload = json.dumps([response, history], ensure_ascii=False).encode()
        if len(payload) > self.MAX_ENTRY_BYTES:
            raise ResponsesError("会话记录过大，未保存续接记录；请缩短输入后重试。")
        try:
            with self.lock, self.db:
                self.prune()
                self.db.execute("DELETE FROM responses WHERE id = ?", (key,))
                total = self.db.execute("SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM responses").fetchone()[0]
                for old_id, size in self.db.execute("SELECT id, LENGTH(payload) FROM responses WHERE checkpoint = 0 ORDER BY created").fetchall():
                    if total + len(payload) <= self.MAX_TOTAL_BYTES:
                        break
                    self.db.execute("DELETE FROM responses WHERE id = ?", (old_id,))
                    total -= size
                if total + len(payload) > self.MAX_TOTAL_BYTES:
                    raise ResponsesError("会话存储容量不足，原有摘要已保留；请清理不再需要的记录。")
                self.db.execute("INSERT INTO responses VALUES (?, ?, ?, ?)",
                                (key, time.time(), key.startswith("bridge_compact_"), payload))
        except sqlite3.Error as exc:
            raise ResponsesError("会话记录保存失败，请检查本地存储空间。") from exc

    def prune(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM responses WHERE checkpoint = 0 AND created < ?", (time.time() - self.ttl_seconds,))

    def get(self, key: str) -> tuple[dict, list]:
        if not isinstance(key, str):
            raise ResponsesError("会话记录编号必须是文字。")
        with self.lock, self.db:
            self.prune()
            entry = self.db.execute("SELECT payload FROM responses WHERE id = ?", (key,)).fetchone()
        if entry is None:
            raise ResponsesError("会话记录不存在或已过期，请发送完整对话。")
        response, history = json.loads(entry[0])
        return response, history

    def delete(self, key: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM responses WHERE id = ?", (key,)).rowcount > 0

    def close(self) -> None:
        with self.lock:
            self.db.close()


def normalize_tools(raw_tools: list) -> tuple[list, dict]:
    tools, mapping = [], {}

    def add(tool: dict, namespace: str | None = None) -> None:
        kind = tool.get("type", "function")
        if kind == "namespace":
            if namespace or not isinstance(tool.get("name"), str) or not tool["name"] or not isinstance(tool.get("tools"), list):
                raise ResponsesError("工具分组格式不正确。")
            for child in tool["tools"]:
                if not isinstance(child, dict):
                    raise ResponsesError("分组中的工具定义必须是对象。")
                add(child, tool.get("name"))
            return
        # Codex includes hosted search by default; keep local browser tools instead.
        if kind in {"web_search", "web_search_preview"}:
            return
        if kind == "image_generation":
            add({"type": "function", "name": "bridge_image_generation", "description": "Generate or edit an actual image. Call this tool when an image artifact is requested. Supply a complete visual prompt.",
                 "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"], "additionalProperties": False}})
            mapping["bridge_image_generation"]["type"] = "image_generation"
            return
        if kind not in {"function", "custom"}:
            raise ResponsesError(f"暂不支持服务端工具 {kind}；请通过 Codex 的本地工具提供此能力。")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesError("工具名称不能为空。")
        wire_name = f"{namespace}.{name}" if namespace else name
        if wire_name in mapping:
            raise ResponsesError(f"工具名称重复：{wire_name}")
        mapping[wire_name] = {"type": kind, "name": name, **({"namespace": namespace} if namespace else {})}
        parameters = tool.get("parameters") or {"type": "object", "properties": {}}
        description = tool.get("description", "")
        if not isinstance(parameters, dict) or not isinstance(description, str):
            raise ResponsesError("工具的 parameters 必须是对象，description 必须是文字。")
        if kind == "custom":
            parameters = {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"], "additionalProperties": False}
            description += "\nPass the EXACT free-form tool input as the JSON string `input`. Preserve all newlines."
            if tool.get("format"):
                description += "\nInput format: " + json.dumps(tool["format"], ensure_ascii=False)
        tools.append({"type": "function", "function": {"name": wire_name, "description": description, "parameters": parameters}})

    if not isinstance(raw_tools, list):
        raise ResponsesError("tools 必须是数组。")
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ResponsesError("工具定义必须是对象。")
        add(tool)
    return tools, mapping


def content_parts(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesError("消息内容必须是文字或内容数组。")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise ResponsesError("消息内容项必须是对象。")
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text"}:
            if not isinstance(part.get("text"), str):
                raise ResponsesError("text 必须是文字。")
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind in {"input_image", "image_url"}:
            raw_img = part.get("image_url")
            url = raw_img.get("url") if isinstance(raw_img, dict) else raw_img
            fid = part.get("file_id") or (raw_img.get("file_id") if isinstance(raw_img, dict) else None)
            if not fid and not isinstance(url, str):
                raise ResponsesError("图片请提供 image_url（data URL 或本地文件链接）或已上传的 file_id。")
            img_obj = {"detail": part.get("detail", "auto")}
            if isinstance(url, str) and url:
                img_obj["url"] = url
            if isinstance(fid, str) and fid:
                img_obj["file_id"] = fid
                img_obj.setdefault("url", fid)
            parts.append({"type": "image_url", "image_url": img_obj})
        elif kind in {"input_file", "file", "document"}:
            inner = part.get("file") if isinstance(part.get("file"), dict) else part
            fid = inner.get("file_id") or part.get("file_id")
            fdata = inner.get("file_data") or inner.get("file_url") or inner.get("data") or inner.get("url")
            fname = inner.get("filename") or part.get("filename")
            mime = inner.get("mime_type") or part.get("mime_type")
            if not fid and not isinstance(fdata, str):
                raise ResponsesError("文件请提供 file_data（base64 或 data URL）或已上传的 file_id。")
            file_obj = {}
            if fid:
                file_obj["file_id"] = fid
            if fdata:
                file_obj["file_data"] = fdata
            if fname:
                file_obj["filename"] = fname
            if mime:
                file_obj["mime_type"] = mime
            parts.append({"type": "file", "file": file_obj})
        elif kind in {"input_audio", "audio_url", "audio"}:
            inner = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else (
                part.get("audio_url") if isinstance(part.get("audio_url"), dict) else part
            )
            fid = inner.get("file_id") or part.get("file_id")
            adata = inner.get("data") or inner.get("audio_url") or inner.get("url")
            fmt = inner.get("format") or part.get("format")
            fname = inner.get("filename") or part.get("filename")
            if not fid and not isinstance(adata, str):
                raise ResponsesError("音频请提供 data / audio_url 或已上传的 file_id。")
            audio_obj = {"type": "input_audio"}
            if fid:
                audio_obj["file_id"] = fid
            if adata:
                audio_obj["data"] = adata
            if fmt:
                audio_obj["format"] = fmt
            if fname:
                audio_obj["filename"] = fname
            parts.append(audio_obj)
        elif kind in {"input_video", "video_url", "video"}:
            inner = part.get("video_url") if isinstance(part.get("video_url"), dict) else part
            fid = inner.get("file_id") or part.get("file_id")
            vdata = inner.get("url") or inner.get("video_url") or inner.get("file_data") or inner.get("data")
            fname = inner.get("filename") or part.get("filename")
            mime = inner.get("mime_type") or part.get("mime_type")
            if not fid and not isinstance(vdata, str):
                raise ResponsesError("视频请提供 video_url / file_data 或已上传的 file_id。")
            vid_obj = {"type": "video_url"}
            if fid:
                vid_obj["file_id"] = fid
            if vdata:
                vid_obj["video_url"] = {"url": vdata}
            if fname:
                vid_obj["filename"] = fname
            if mime:
                vid_obj["mime_type"] = mime
            parts.append(vid_obj)
        elif kind == "refusal":
            parts.append(part)
        else:
            raise ResponsesError(f"暂不支持输入内容类型：{kind}")
    existing_text = "\n".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
    for part in content:
        if isinstance(part, dict) and part.get("type") in {
            "input_image", "image_url", "input_file", "file", "document",
            "input_audio", "audio_url", "audio", "input_video", "video_url", "video",
        }:
            marker = extract_part_marker(part)
            if marker and marker not in existing_text:
                parts.append({"type": "text", "text": marker})
                existing_text += "\n" + marker
    return parts


def input_messages(items: Any, mapping: dict, store: ResponseStore) -> list[ChatMessage]:
    if isinstance(items, str):
        return [ChatMessage(role="user", content=items)]
    if not isinstance(items, list):
        raise ResponsesError("input 必须是文字或消息数组。")
    messages = []
    for item in items:
        if not isinstance(item, dict):
            raise ResponsesError("input 内容项必须是对象。")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role", "user")
            if role not in {"user", "assistant", "system", "developer"}:
                raise ResponsesError(f"不支持消息角色：{role}")
            messages.append(ChatMessage(role=role, content=content_parts(item.get("content", ""))))
        elif kind in {"function_call", "custom_tool_call"}:
            name = item.get("name", "")
            if item.get("namespace"):
                name = f'{item["namespace"]}.{name}'
            args = item.get("arguments", "{}") if kind == "function_call" else json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
            messages.append(ChatMessage(role="assistant", tool_calls=[{"id": item.get("call_id"), "type": "function", "function": {"name": name, "arguments": args}}]))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output", "")
            if isinstance(output, list):
                output = content_parts(output)
            elif not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append(ChatMessage(role="tool", content=output, tool_call_id=item.get("call_id")))
        elif kind == "image_generation_call":
            messages.append(ChatMessage(role="assistant", content="[Image generated successfully.]"))
        elif kind == "reasoning":
            # Reasoning is private provider state, not user-visible conversation.
            continue
        elif kind == "compaction":
            _, compacted = store.get(item.get("encrypted_content", ""))
            messages.extend(ChatMessage(**msg) for msg in compacted)
        elif kind == "item_reference":
            raise ResponsesError("请发送完整消息内容，不支持 item_reference。")
        else:
            raise ResponsesError(f"暂不支持输入项类型：{kind}")
    merged = []
    for message in messages:
        if merged and message.role == "assistant" and merged[-1].role == "assistant":
            previous = merged[-1]
            texts = [previous.text_content(), message.text_content()]
            previous.content = "\n".join(text for text in texts if text) or None
            previous.tool_calls = (previous.tool_calls or []) + (message.tool_calls or []) or None
        else:
            merged.append(message)
    return merged


def prepare_request(payload: dict, store: ResponseStore) -> tuple[ChatCompletionRequest, dict, list]:
    for field, expected in {"model": str, "instructions": str, "previous_response_id": str, "prompt_cache_key": str,
                            "text": dict, "metadata": dict, "tools": list, "stream": bool, "store": bool}.items():
        if payload.get(field) is not None and not isinstance(payload[field], expected):
            raise ResponsesError(f"{field} 格式不正确。")
    if payload.get("background"):
        raise ResponsesError("本地桥接暂不支持 background，请保持请求连接。")
    tools, mapping = normalize_tools(payload.get("tools") or [])
    messages = []
    previous = {}
    if payload.get("previous_response_id"):
        previous, history = store.get(payload["previous_response_id"])
        # Only top-level instructions expire; explicit developer messages remain conversation history.
        if previous.get("instructions") and history and history[0].get("role") == "developer" and history[0].get("content") == previous["instructions"]:
            history = history[1:]
        messages.extend(ChatMessage(**m) for m in history)
    if payload.get("instructions"):
        messages.insert(0, ChatMessage(role="developer", content=payload["instructions"]))
    messages.extend(input_messages(payload.get("input", []), mapping, store))
    if not messages:
        raise ResponsesError("input 不能为空。")
    choice = payload.get("tool_choice", "auto")
    if not isinstance(choice, (str, dict)) or (isinstance(choice, str) and choice not in {"auto", "none", "required"}):
        raise ResponsesError("tool_choice 格式不正确。")
    if choice == "required" and not tools:
        raise ResponsesError("tool_choice=required 需要声明工具。")
    if isinstance(choice, dict):
        if choice.get("type") == "image_generation":
            choice = {"type": "function", "name": "bridge_image_generation"}
        if choice.get("type") not in {"function", "custom"}:
            raise ResponsesError("此 tool_choice 类型暂不支持。")
        name = choice.get("name", "")
        if choice.get("namespace"):
            name = f'{choice["namespace"]}.{name}'
        if name not in mapping:
            raise ResponsesError(f"tool_choice 指定了未声明工具：{name}")
        choice = {"type": "function", "function": {"name": name}}
    text_format = (payload.get("text") or {}).get("format")
    if text_format is not None and not isinstance(text_format, dict):
        raise ResponsesError("text.format 必须是对象。")
    response_format = None
    if isinstance(text_format, dict) and text_format.get("type") != "text":
        if text_format.get("type") == "json_schema":
            response_format = {"type": "json_schema", "json_schema": {k: v for k, v in text_format.items() if k != "type"}}
        elif text_format.get("type") == "json_object":
            response_format = text_format
        else:
            raise ResponsesError("不支持指定的文字输出格式。")
    req = ChatCompletionRequest(model=payload.get("model") or previous.get("model") or "", messages=messages, stream=bool(payload.get("stream")),
        stream_options={"include_usage": True}, tools=tools, tool_choice=choice,
        parallel_tool_calls=payload.get("parallel_tool_calls", True), response_format=response_format,
        max_completion_tokens=payload.get("max_output_tokens"), temperature=payload.get("temperature"), top_p=payload.get("top_p"),
        session_id=payload.get("prompt_cache_key"))
    return req, mapping, [m.model_dump(exclude_none=True) for m in messages]


def output_call(call: dict, mapping: dict) -> dict:
    fn = call.get("function", {})
    spec = mapping.get(fn.get("name"), {"type": "function", "name": fn.get("name", "")})
    result = {"id": "fc_" + uuid.uuid4().hex, "call_id": call.get("id") or "call_" + uuid.uuid4().hex,
              "type": "custom_tool_call" if spec["type"] == "custom" else "function_call",
              "status": "completed", "name": spec["name"]}
    if spec["type"] == "image_generation":
        args = json.loads(fn.get("arguments") or "{}")
        return {"id": "ig_" + uuid.uuid4().hex, "type": "image_generation_call", "status": "in_progress", "result": None, "_prompt": args.get("prompt", "")}
    if spec.get("namespace"):
        result["namespace"] = spec["namespace"]
    if spec["type"] == "custom":
        args = json.loads(fn.get("arguments") or "{}")
        if not isinstance(args, dict) or not isinstance(args.get("input"), str):
            raise ResponsesError("自由文本工具缺少 input 文字。")
        result["input"] = args["input"]
    else:
        result["arguments"] = fn.get("arguments") or "{}"
    return result


def response_base(payload: dict, model: str) -> dict:
    return {"id": "resp_bridge_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "model": model, "output": [], "error": None, "incomplete_details": None,
            "parallel_tool_calls": payload.get("parallel_tool_calls", True), "tool_choice": payload.get("tool_choice", "auto"),
            "tools": payload.get("tools", []), "instructions": payload.get("instructions"), "metadata": payload.get("metadata") or {},
            "usage": None, "store": payload.get("store", True)}


def usage_from_chat(usage: dict) -> dict:
    return {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0), "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def remember(response: dict, history: list, store: ResponseStore) -> None:
    if not response["store"]:
        return
    from media_inputs import compact_messages_for_store
    messages = input_messages(response["output"], {}, store)
    combined = compact_messages_for_store(
        history + [m.model_dump(exclude_none=True) for m in messages],
        getattr(store, "file_store", None),
    )
    store.put(response["id"], response, combined)


async def chat_packets(body: AsyncIterator) -> AsyncIterator[dict]:
    buffer = ""
    async for piece in body:
        buffer += piece.decode() if isinstance(piece, bytes) else piece
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data == "[DONE]":
                return
            if data:
                yield json.loads(data)
            elif block.startswith(":"):
                yield {}


async def translate_response(upstream: Any, payload: dict, model: str, mapping: dict, history: list, store: ResponseStore, image_generator=None) -> Any:
    async def resolve_image(item):
        if image_generator is None:
            raise ResponsesError("生图执行器未连接。")
        prompt = item.pop("_prompt")
        result = await image_generator(prompt)
        item.update(status="completed", result=result["data"][0]["b64_json"])
        return item
    if isinstance(upstream, JSONResponse):
        if upstream.status_code >= 400:
            return upstream
        upstream = json.loads(upstream.body)
    response = response_base(payload, model)
    if not isinstance(upstream, StreamingResponse):
        msg = upstream["choices"][0]["message"]
        annotations = [
            {"type": "file_path", "file_id": a.get("file_id"), "filename": a.get("filename"),
             "mime_type": a.get("mime_type"), "url": a.get("url"), "path": a.get("path")}
            for a in (msg.get("attachments") or []) if isinstance(a, dict)
        ]
        if msg.get("content") or annotations:
            response["output"].append({"id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": msg.get("content") or "", "annotations": annotations}]})
        for tc in msg.get("tool_calls", []):
            item = output_call(tc, mapping)
            if item["type"] == "image_generation_call":
                item = await resolve_image(item)
            response["output"].append(item)
        response.update(status="completed", usage=usage_from_chat(upstream.get("usage", {})))
        remember(response, history, store)
        return JSONResponse(response)

    async def events() -> AsyncIterator[str]:
        sequence = 0
        def event(kind: str, **data: Any) -> str:
            nonlocal sequence
            packet = {"type": kind, "sequence_number": sequence, **data}
            sequence += 1
            return f"event: {kind}\ndata: {json.dumps(packet, ensure_ascii=False)}\n\n"
        yield event("response.created", response=response)
        yield event("response.in_progress", response=response)
        message = None
        calls: dict[int, dict] = {}
        finished = False
        try:
            async for packet in chat_packets(upstream.body_iterator):
                if not packet:
                    yield ": keepalive\n\n"
                    continue
                if packet.get("error"):
                    raise ResponsesError(packet["error"]["message"])
                if packet.get("usage"):
                    response["usage"] = usage_from_chat(packet["usage"])
                for choice in packet.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("attachments"):
                        ann_list = [
                            {"type": "file_path", "file_id": a.get("file_id"), "filename": a.get("filename"),
                             "mime_type": a.get("mime_type"), "url": a.get("url"), "path": a.get("path")}
                            for a in delta["attachments"] if isinstance(a, dict)
                        ]
                        if message is None:
                            message = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                            response["output"].append(message)
                            yield event("response.output_item.added", output_index=0, item=dict(message))
                            part = {"type": "output_text", "text": "", "annotations": []}
                            message["content"].append(part)
                            yield event("response.content_part.added", item_id=message["id"], output_index=0, content_index=0, part=dict(part))
                        message["content"][0]["annotations"].extend(ann_list)
                    text = delta.get("content")
                    if text:
                        if message is None:
                            message = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                            response["output"].append(message)
                            yield event("response.output_item.added", output_index=0, item=dict(message))
                            part = {"type": "output_text", "text": "", "annotations": []}
                            message["content"].append(part)
                            yield event("response.content_part.added", item_id=message["id"], output_index=0, content_index=0, part=dict(part))
                        message["content"][0]["text"] += text
                        yield event("response.output_text.delta", item_id=message["id"], output_index=0, content_index=0, delta=text, logprobs=[])
                    for tc in delta.get("tool_calls", []):
                        call = calls.setdefault(tc["index"], {"id": "", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            call["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        for key in ("name", "arguments"):
                            call["function"][key] += fn.get(key) or ""
                    if choice.get("finish_reason"):
                        finished = True
            if not finished:
                raise ResponsesError("上游连接提前结束，未收到完整回复。")
            if message:
                message["status"] = "completed"
                yield event("response.output_text.done", item_id=message["id"], output_index=0, content_index=0, text=message["content"][0]["text"], logprobs=[])
                yield event("response.content_part.done", item_id=message["id"], output_index=0, content_index=0, part=message["content"][0])
                yield event("response.output_item.done", output_index=0, item=message)
            for index in sorted(calls):
                item = output_call(calls[index], mapping)
                output_index = len(response["output"])
                response["output"].append(item)
                if item["type"] == "image_generation_call":
                    yield event("response.output_item.added", output_index=output_index, item={k: v for k, v in item.items() if k != "_prompt"})
                    await resolve_image(item)
                    yield event("response.output_item.done", output_index=output_index, item=item)
                    continue
                key = "input" if item["type"] == "custom_tool_call" else "arguments"
                initial = {**item, "status": "in_progress", key: ""}
                yield event("response.output_item.added", output_index=output_index, item=initial)
                event_prefix = "response.custom_tool_call_input" if key == "input" else "response.function_call_arguments"
                value = item[key]
                for start in range(0, len(value), 12000):
                    yield event(event_prefix + ".delta", item_id=item["id"], output_index=output_index, delta=value[start:start+12000])
                yield event(event_prefix + ".done", item_id=item["id"], output_index=output_index, **{key: value})
                yield event("response.output_item.done", output_index=output_index, item=item)
            response["status"] = "completed"
            response["usage"] = response["usage"] or usage_from_chat({})
            remember(response, history, store)
            yield event("response.completed", response=response)
        except Exception as exc:
            response.update(status="failed", error={"code": "bridge_error", "message": str(exc)})
            yield event("response.failed", response=response)
        finally:
            await upstream.body_iterator.aclose()
    return StreamingResponse(with_keepalive(events()), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

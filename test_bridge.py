"""Automated pytest suite for Gemini Enterprise Web-to-API Bridge."""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi.testclient import TestClient

from config import BridgeSettings
from context_builder import ConversationContextBuilder
from extension_exporter import export_extension_bundle, render_spark_icon_png
from schemas import (
    ChatMessage,
    extract_text_content,
    model_requests_new_chat,
    resolve_target_web_model,
)
from server import create_app


# ---------------------------------------------------------------------------
# 1. Schema, Context Builder, and Extension Icon Bundle Unit Tests
# ---------------------------------------------------------------------------


def test_extract_text_content_string_and_array() -> None:
    assert extract_text_content("你好 Gemini") == "你好 Gemini"
    multimodal = [
        {"type": "text", "text": "第一段说明"},
        {"type": "input_text", "text": "第二段代码"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    text = extract_text_content(multimodal)
    assert "第一段说明" in text
    assert "第二段代码" in text
    assert "Image attachment omitted" in text


def test_smart_context_builder_incremental_and_packed() -> None:
    builder = ConversationContextBuilder(default_mode="smart", auto_reset_new_chat=False)
    session = "ws-test:tab1"

    # Turn 1: system + first user message
    t1_msgs = [
        ChatMessage(role="system", content="你是一名企业架构师。"),
        ChatMessage(role="user", content="请介绍 Vertex AI Search。"),
    ]
    p1 = builder.build(t1_msgs, model="gemini-enterprise-web", session_key=session)
    assert p1.strategy_used == "smart_single_turn"
    assert "你是一名企业架构师" in p1.prompt
    assert "请介绍 Vertex AI Search" in p1.prompt
    assert p1.reset_chat is False

    builder.record_completed_turn(session, p1.normalized_history, "Vertex AI Search 是企业级检索与生成平台。")

    # Turn 2: matching history -> sends ONLY the new follow-up message!
    t2_msgs = [
        ChatMessage(role="system", content="你是一名企业架构师。"),
        ChatMessage(role="user", content="请介绍 Vertex AI Search。"),
        ChatMessage(role="assistant", content="Vertex AI Search 是企业级检索与生成平台。"),
        ChatMessage(role="user", content="它与 Agentspace 是什么关系？"),
    ]
    p2 = builder.build(t2_msgs, model="gemini-enterprise-web", session_key=session)
    assert p2.strategy_used == "smart_incremental_delta"
    assert p2.prompt == "它与 Agentspace 是什么关系？"
    assert p2.reset_chat is False

    # Unseen multi-turn thread -> packs context cleanly
    other_msgs = [
        ChatMessage(role="user", content="前情提要 A"),
        ChatMessage(role="assistant", content="回复 A"),
        ChatMessage(role="user", content="追问 B"),
    ]
    p3 = builder.build(other_msgs, model="gemini-enterprise-web-newchat", session_key=session)
    assert p3.strategy_used == "smart_packed_context"
    assert p3.reset_chat is True
    assert "前情提要 A" in p3.prompt
    assert "追问 B" in p3.prompt


def test_png_icon_generation_and_bundle_export(tmp_path: Path) -> None:
    for size in (16, 48, 128):
        png_bytes = render_spark_icon_png(size)
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", png_bytes[16:24])
        assert (width, height) == (size, size)

    # Simulate root files in tmp_path and verify export_extension_bundle
    (tmp_path / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Test", "version": "1.0.0", "action": {}}),
        encoding="utf-8",
    )
    for fname in ("background.js", "content-bridge.js", "page-driver.js", "popup.html", "popup.css", "popup.js"):
        (tmp_path / fname).write_text("// stub", encoding="utf-8")
    (tmp_path / "gemini-enterprise-bridge.user.js").write_text("// userscript", encoding="utf-8")

    ext_dir = export_extension_bundle(tmp_path)
    exported_manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert exported_manifest["icons"]["128"] == "icons/icon-128.png"
    assert (ext_dir / "icons" / "icon-16.png").is_file()
    assert (ext_dir / "icons" / "icon-48.png").is_file()
    assert (ext_dir / "icons" / "icon-128.png").is_file()
    assert (tmp_path / "userscript" / "gemini-enterprise-bridge.user.js").is_file()


# ---------------------------------------------------------------------------
# 2. FastAPI OpenAI Endpoints + Simulated WebSocket Extension E2E Tests
# ---------------------------------------------------------------------------


def _make_test_settings(**overrides: Any) -> BridgeSettings:
    defaults: Dict[str, Any] = {
        "auto_export_extension": False,
        "request_timeout_seconds": 10.0,
        "queue_timeout_seconds": 10.0,
    }
    defaults.update(overrides)
    return BridgeSettings(**defaults)


def test_health_models_and_503_when_disconnected() -> None:
    app = create_app(_make_test_settings())
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["browser_connected"] is False

        models = client.get("/v1/models").json()
        assert models["object"] == "list"
        model_ids = {m["id"] for m in models["data"]}
        assert "gemini-enterprise-web" in model_ids

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gemini-enterprise-web", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "browser_not_connected"


def test_api_key_authentication_when_enabled() -> None:
    app = create_app(_make_test_settings(api_key="sk-secret-enterprise"))
    with TestClient(app) as client:
        unauth = client.get("/v1/models")
        assert unauth.status_code == 401

        auth_ok = client.get("/v1/models", headers={"Authorization": "Bearer sk-secret-enterprise"})
        assert auth_ok.status_code == 200


def test_websocket_browser_sync_and_sse_streaming_roundtrip() -> None:
    app = create_app(_make_test_settings())
    received_prompts: List[str] = []

    with TestClient(app) as client:
        ready_event = threading.Event()
        stop_flag = False

        with client.websocket_connect("/ws/browser?client_id=ext-e2e") as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 42,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/abc",
                    "tab_title": "Agentspace Enterprise",
                    "ready": True,
                    "driver_mode": "auto",
                    "captured_rpc": True,
                }
            )
            ack = ws.receive_json()
            assert ack["type"] == "ack"

            def browser_worker() -> None:
                ready_event.set()
                # Process 2 sequential requests (1 non-streaming, 1 streaming)
                for idx in range(2):
                    if stop_flag:
                        break
                    cmd = ws.receive_json()
                    assert cmd["type"] == "execute_prompt"
                    req_id = cmd["request_id"]
                    received_prompts.append(cmd["prompt"])
                    ws.send_json({"type": "request_ack", "request_id": req_id})

                    if idx == 0:
                        ws.send_json(
                            {
                                "type": "request_complete",
                                "request_id": req_id,
                                "reply": "这是第一轮企业知识库回答。",
                                "citations": [
                                    {"title": "内部架构规范 v2", "url": "https://intranet.example.com/spec"}
                                ],
                                "source": "network_stream",
                            }
                        )
                    else:
                        for piece in ("流式片段一，", "流式片段二，", "流式完成！"):
                            ws.send_json(
                                {"type": "stream_chunk", "request_id": req_id, "delta": piece}
                            )
                        ws.send_json(
                            {
                                "type": "request_complete",
                                "request_id": req_id,
                                "reply": "流式片段一，流式片段二，流式完成！",
                                "source": "network_stream",
                            }
                        )

            worker = threading.Thread(target=browser_worker, daemon=True)
            worker.start()
            ready_event.wait(timeout=3.0)

            # Request 1: Non-streaming with OpenAI array content + grounding citation
            r1 = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-enterprise-web",
                    "stream": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "查询内部架构规范"}],
                        }
                    ],
                },
            )
            assert r1.status_code == 200
            data1 = r1.json()
            assert data1["object"] == "chat.completion"
            content1 = data1["choices"][0]["message"]["content"]
            assert "这是第一轮企业知识库回答。" in content1
            assert "https://intranet.example.com/spec" in content1
            assert data1["usage"]["total_tokens"] > 0

            # Request 2: Multi-turn SSE streaming (verifies smart delta + [DONE] + usage)
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gemini-enterprise-web",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [
                        {"role": "user", "content": "查询内部架构规范"},
                        {"role": "assistant", "content": content1},
                        {"role": "user", "content": "请继续展开细节"},
                    ],
                },
            ) as stream_resp:
                assert stream_resp.status_code == 200
                raw_lines = [line for line in stream_resp.iter_lines() if line]

            worker.join(timeout=3.0)

        # Smart delta should have sent ONLY the new follow-up message on turn 2!
        assert received_prompts == ["查询内部架构规范", "请继续展开细节"]

        assert raw_lines[-1] == "data: [DONE]"
        assembled = ""
        saw_stop = False
        saw_usage = False
        for line in raw_lines[:-1]:
            assert line.startswith("data: ")
            chunk = json.loads(line[6:])
            assert chunk["object"] == "chat.completion.chunk"
            if chunk.get("usage"):
                saw_usage = True
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    assembled += delta["content"]
                if choice.get("finish_reason") == "stop":
                    saw_stop = True

        assert assembled == "流式片段一，流式片段二，流式完成！"
        assert saw_stop is True
        assert saw_usage is True


# ---------------------------------------------------------------------------
# 3. Concurrency FIFO Serialization & Tampermonkey HTTP Bridge Test
# ---------------------------------------------------------------------------


def test_http_poll_bridge_and_concurrent_fifo_serialization() -> None:
    async def _run() -> None:
        app = create_app(_make_test_settings())
        transport = httpx.ASGITransport(app=app)
        execution_windows: List[tuple[float, float]] = []

        async with httpx.AsyncClient(transport=transport, base_url="http://bridge.local") as client:
            reg = await client.post(
                "/api/bridge/register",
                json={
                    "client_id": "tm-test-1",
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                    "tab_title": "Vertex AI Search",
                },
            )
            assert reg.status_code == 200

            async def simulated_tampermonkey() -> None:
                for _ in range(2):
                    poll_res = await client.get(
                        "/api/bridge/poll",
                        params={"client_id": "tm-test-1", "timeout": 5.0},
                    )
                    cmd = poll_res.json()["command"]
                    assert cmd is not None
                    t_start = time.monotonic()
                    await asyncio.sleep(0.08)
                    t_end = time.monotonic()
                    execution_windows.append((t_start, t_end))
                    await client.post(
                        "/api/bridge/event",
                        json={
                            "client_id": "tm-test-1",
                            "event": "request_complete",
                            "request_id": cmd["request_id"],
                            "reply": f"Echo: {cmd['prompt']}",
                        },
                    )

            tm_task = asyncio.create_task(simulated_tampermonkey())

            # Fire two concurrent chat completion calls simultaneously
            resp_a, resp_b = await asyncio.gather(
                client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Task-A"}]},
                ),
                client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Task-B"}]},
                ),
            )
            await tm_task

            assert resp_a.status_code == 200
            assert resp_b.status_code == 200
            replies = {
                resp_a.json()["choices"][0]["message"]["content"],
                resp_b.json()["choices"][0]["message"]["content"],
            }
            assert replies == {"Echo: Task-A", "Echo: Task-B"}

            # Verify zero overlap between the two browser executions (FIFO lock enforced)
            assert len(execution_windows) == 2
            first_end = execution_windows[0][1]
            second_start = execution_windows[1][0]
            assert second_start >= first_end - 1e-6

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Direct Web Model Selection (3.1 Pro / 4 Argon / 3.8 Flash / Custom) Tests
# ---------------------------------------------------------------------------


def test_model_alias_resolution_and_models_endpoint() -> None:
    assert resolve_target_web_model("gemini-enterprise-web") is None
    assert resolve_target_web_model("gemini-enterprise-web-newchat") is None
    assert resolve_target_web_model("gemini-enterprise-gcloud") is None

    for alias in ("3.1pro", "3.1-pro", "gemini-3.1-pro", "gemini-3.1-pro-newchat"):
        assert resolve_target_web_model(alias) == "3.1-pro"
    for alias in ("4argon", "4-argon", "argon", "gemini-4-argon", "4argon-newchat"):
        assert resolve_target_web_model(alias) == "4-argon"
    for alias in ("3.8flash", "3.8-flash", "gemini-3.8-flash", "gemini-3.8-flash-newchat"):
        assert resolve_target_web_model(alias) == "3.8-flash"

    assert resolve_target_web_model("2.5pro") == "2.5-pro"
    assert resolve_target_web_model("2.5flash") == "2.5-flash"
    assert resolve_target_web_model("gemini-web:Gemini 4.5 Ultra") == "Gemini 4.5 Ultra"
    assert resolve_target_web_model("gemini-4.2-pro") == "4.2-pro"

    assert model_requests_new_chat("gemini-4-argon-newchat") is True
    assert model_requests_new_chat("gemini-4-argon") is False

    app = create_app(_make_test_settings())
    dispatched_commands: List[Dict[str, Any]] = []

    with TestClient(app) as client:
        models_resp = client.get("/v1/models").json()
        listed_ids = {m["id"] for m in models_resp["data"]}
        for expected_id in (
            "gemini-3.1-pro",
            "gemini-3.1-pro-newchat",
            "gemini-4-argon",
            "gemini-4-argon-newchat",
            "gemini-3.8-flash",
            "gemini-3.8-flash-newchat",
        ):
            assert expected_id in listed_ids

        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-models") as ws:
            ws.receive_json()  # welcome
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 7,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home",
                    "ready": True,
                }
            )
            ws.receive_json()  # ack

            def model_worker() -> None:
                ready.set()
                for _ in range(3):
                    cmd = ws.receive_json()
                    dispatched_commands.append(cmd)
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "reply": f"Handled by {cmd.get('target_model')}",
                            "active_model": cmd.get("target_model") or "default",
                        }
                    )

            t = threading.Thread(target=model_worker, daemon=True)
            t.start()
            ready.wait(timeout=3.0)

            for req_model in ("3.1pro", "gemini-4-argon", "3.8flash-newchat"):
                r = client.post(
                    "/v1/chat/completions",
                    json={"model": req_model, "messages": [{"role": "user", "content": "Ping"}]},
                )
                assert r.status_code == 200

            t.join(timeout=3.0)

            status_data = client.get("/api/status").json()
            assert status_data["hub"]["active_client"]["active_model"] == "3.8-flash"

    assert [c["target_model"] for c in dispatched_commands] == ["3.1-pro", "4-argon", "3.8-flash"]
    # With auto_reset_new_chat=True by default, each brand-new single-turn local conversation starts a clean web session
    assert [c["reset_chat"] for c in dispatched_commands] == [True, True, True]


def test_local_session_switch_creates_new_web_session_automatically() -> None:
    """Verify local session A (turns 1-2) -> local session B (turns 1-2) -> back to session A (turns 3-4)."""
    app = create_app(_make_test_settings())
    commands: List[Dict[str, Any]] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-multi-session") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 55,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/abc/r/session/old999",
                    "ready": True,
                }
            )
            ws.receive_json()

            def worker() -> None:
                ready.set()
                for idx in range(6):
                    cmd = ws.receive_json()
                    commands.append(cmd)
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "reply": f"Reply-{idx + 1}",
                            "active_model": cmd.get("target_model") or "4-argon",
                        }
                    )

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            # 1) Local Session A, Turn 1 (even on first request, resets any pre-existing web `/r/session/old999`)
            sess_a: List[Dict[str, Any]] = [
                {"role": "system", "content": "Persona A"},
                {"role": "user", "content": "Session A - Q1"},
            ]
            r_a1 = client.post("/v1/chat/completions", json={"model": "4argon", "messages": sess_a})
            assert r_a1.status_code == 200
            sess_a.append({"role": "assistant", "content": r_a1.json()["choices"][0]["message"]["content"]})

            # 2) Local Session A, Turn 2 -> continues inside SAME web session (`reset_chat=False`)
            sess_a.append({"role": "user", "content": "Session A - Q2"})
            r_a2 = client.post("/v1/chat/completions", json={"model": "4argon", "messages": sess_a})
            assert r_a2.status_code == 200
            sess_a.append({"role": "assistant", "content": r_a2.json()["choices"][0]["message"]["content"]})

            # 3) User opens brand-new Local Session B, Turn 1 (with normal `gemini-4-argon`, NO `-newchat` needed!)
            sess_b: List[Dict[str, Any]] = [
                {"role": "system", "content": "Persona B"},
                {"role": "user", "content": "Session B - Q1"},
            ]
            r_b1 = client.post("/v1/chat/completions", json={"model": "gemini-4-argon", "messages": sess_b})
            assert r_b1.status_code == 200
            sess_b.append({"role": "assistant", "content": r_b1.json()["choices"][0]["message"]["content"]})

            # 4) Local Session B, Turn 2 -> continues inside Session B's web session (`reset_chat=False`)
            sess_b.append({"role": "user", "content": "Session B - Q2"})
            r_b2 = client.post("/v1/chat/completions", json={"model": "gemini-4-argon", "messages": sess_b})
            assert r_b2.status_code == 200
            sess_b.append({"role": "assistant", "content": r_b2.json()["choices"][0]["message"]["content"]})

            # 5) User switches back to historical Local Session A in client sidebar -> Turn 3 (`reset_chat=True` + packed history)
            sess_a.append({"role": "user", "content": "Session A - Q3"})
            r_a3 = client.post("/v1/chat/completions", json={"model": "4argon", "messages": sess_a})
            assert r_a3.status_code == 200
            sess_a.append({"role": "assistant", "content": r_a3.json()["choices"][0]["message"]["content"]})

            # 6) Local Session A, Turn 4 -> continues incrementally again (`reset_chat=False`)
            sess_a.append({"role": "user", "content": "Session A - Q4"})
            r_a4 = client.post("/v1/chat/completions", json={"model": "4argon", "messages": sess_a})
            assert r_a4.status_code == 200

            th.join(timeout=3.0)

    assert [c["reset_chat"] for c in commands] == [
        True,   # 1: Session A Turn 1 -> new web session
        False,  # 2: Session A Turn 2 -> same web session (incremental)
        True,   # 3: Session B Turn 1 -> new web session!
        False,  # 4: Session B Turn 2 -> same web session (incremental)
        True,   # 5: Switch back to Session A Turn 3 -> new web session (packed history)
        False,  # 6: Session A Turn 4 -> same web session (incremental)
    ]
    assert commands[1]["prompt"] == "Session A - Q2"
    assert commands[3]["prompt"] == "Session B - Q2"
    assert "[Conversation History]" in commands[4]["prompt"]
    assert commands[5]["prompt"] == "Session A - Q4"


# ---------------------------------------------------------------------------
# 5. OpenAI Tool Calling (Sync + SSE Streaming) & Multi-Turn Agentic Loop Tests
# ---------------------------------------------------------------------------

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Search enterprise policy handbook",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        },
    },
]


def test_non_streaming_tool_calling_and_parallel_control() -> None:
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-tools-sync") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 11,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home",
                    "ready": True,
                }
            )
            ws.receive_json()

            def worker_fn() -> None:
                ready.set()
                # Case 1: Parallel [TOOL_CALL] with parallel_tool_calls=False -> sliced to 1, citations suppressed
                c1 = ws.receive_json()
                assert "[Tool Calling Protocol]" in c1["prompt"]
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c1["request_id"],
                        "reply": (
                            '[TOOL_CALL]{"name": "get_weather", "arguments": {"city": "Hong Kong"}}[/TOOL_CALL]\n'
                            '[TOOL_CALL]{"name": "search_policy", "arguments": {"topic": "travel"}}[/TOOL_CALL]'
                        ),
                        "citations": [{"title": "Should Not Appear", "url": "https://example.com/cite"}],
                    }
                )

                # Case 2: Forced tool_choice where model emits bare JSON arguments directly
                c2 = ws.receive_json()
                assert 'MANDATORY FOR THIS TURN: You MUST call the function "search_policy"' in c2["prompt"]
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c2["request_id"],
                        "reply": '```json\n{"topic": "reimbursement"}\n```',
                    }
                )

            th = threading.Thread(target=worker_fn, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            r1 = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.1-pro",
                    "messages": [{"role": "user", "content": "Check HK weather and travel policy"}],
                    "tools": SAMPLE_TOOLS,
                    "parallel_tool_calls": False,
                },
            )
            assert r1.status_code == 200
            d1 = r1.json()
            choice1 = d1["choices"][0]
            assert choice1["finish_reason"] == "tool_calls"
            assert choice1["message"]["content"] is None
            tcs1 = choice1["message"]["tool_calls"]
            assert len(tcs1) == 1
            assert tcs1[0]["function"]["name"] == "get_weather"
            assert json.loads(tcs1[0]["function"]["arguments"]) == {"city": "Hong Kong"}

            r2 = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.1-pro",
                    "messages": [{"role": "user", "content": "Find reimbursement rules"}],
                    "tools": SAMPLE_TOOLS,
                    "tool_choice": {"type": "function", "function": {"name": "search_policy"}},
                },
            )
            assert r2.status_code == 200
            d2 = r2.json()
            choice2 = d2["choices"][0]
            assert choice2["finish_reason"] == "tool_calls"
            tcs2 = choice2["message"]["tool_calls"]
            assert len(tcs2) == 1
            assert tcs2[0]["function"]["name"] == "search_policy"
            assert json.loads(tcs2[0]["function"]["arguments"]) == {"topic": "reimbursement"}

            th.join(timeout=3.0)


def test_sse_streaming_tool_calls_no_sentinel_leak() -> None:
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-tools-stream") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 12,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home",
                    "ready": True,
                }
            )
            ws.receive_json()

            tiny_chunks = [
                "正在查询天气",
                "...\n",
                "[T",
                "OOL",
                "_CA",
                "LL]",
                '{"na',
                'me": "get_weather", ',
                '"arguments": {"city": "Shenzhen"}}',
                "[/T",
                "OOL_CALL]",
            ]
            full_text = "".join(tiny_chunks)

            def stream_worker() -> None:
                ready.set()
                cmd = ws.receive_json()
                req_id = cmd["request_id"]
                for piece in tiny_chunks:
                    ws.send_json({"type": "stream_chunk", "request_id": req_id, "delta": piece})
                ws.send_json({"type": "request_complete", "request_id": req_id, "reply": full_text})

            th = threading.Thread(target=stream_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.8-flash",
                    "stream": True,
                    "tools": SAMPLE_TOOLS,
                    "messages": [{"role": "user", "content": "深圳今天天气如何？"}],
                },
            ) as resp:
                assert resp.status_code == 200
                lines = [ln for ln in resp.iter_lines() if ln]

            th.join(timeout=3.0)

        assert lines[-1] == "data: [DONE]"
        streamed_content = ""
        streamed_tool_calls: List[Dict[str, Any]] = []
        finish_reasons: List[str] = []

        for ln in lines[:-1]:
            payload = json.loads(ln[6:])
            for ch in payload.get("choices", []):
                delta = ch.get("delta", {})
                if delta.get("content"):
                    streamed_content += delta["content"]
                if delta.get("tool_calls"):
                    streamed_tool_calls.extend(delta["tool_calls"])
                if ch.get("finish_reason"):
                    finish_reasons.append(ch["finish_reason"])

        # Preamble streamed cleanly, and ZERO `[TOOL_CALL]` markup leaked into delta.content!
        assert streamed_content == "正在查询天气...\n"
        assert "[TOOL_CALL]" not in streamed_content
        assert len(streamed_tool_calls) == 1
        assert streamed_tool_calls[0]["index"] == 0
        assert streamed_tool_calls[0]["function"]["name"] == "get_weather"
        assert json.loads(streamed_tool_calls[0]["function"]["arguments"]) == {"city": "Shenzhen"}
        assert finish_reasons == ["tool_calls"]


def test_multi_turn_agentic_loop_incremental_delta() -> None:
    """Verify a 3-turn agentic loop (parallel tool calls -> chained tool call -> final answer) stays in-session."""
    app = create_app(_make_test_settings())
    browser_commands: List[Dict[str, Any]] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-agent-loop") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 99,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/agent1",
                    "ready": True,
                }
            )
            ws.receive_json()

            def loop_worker() -> None:
                ready.set()
                # Step 1: Emit 2 parallel tool calls
                c1 = ws.receive_json()
                browser_commands.append(c1)
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c1["request_id"],
                        "reply": (
                            '[TOOL_CALL]{"name": "get_weather", "arguments": {"city": "Hong Kong"}}[/TOOL_CALL]\n'
                            '[TOOL_CALL]{"name": "search_policy", "arguments": {"topic": "typhoon"}}[/TOOL_CALL]'
                        ),
                    }
                )

                # Step 2: Receive both tool results incrementally -> emit 1 follow-up tool call
                c2 = ws.receive_json()
                browser_commands.append(c2)
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c2["request_id"],
                        "reply": '[TOOL_CALL]{"name": "search_policy", "arguments": {"topic": "wfh_allowance"}}[/TOOL_CALL]',
                    }
                )

                # Step 3: Receive 3rd tool result incrementally -> emit final user-facing synthesis
                c3 = ws.receive_json()
                browser_commands.append(c3)
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c3["request_id"],
                        "reply": "香港今日悬挂八号风球（28°C），按企业台风政策可远程办公并申领津贴。",
                    }
                )

            th = threading.Thread(target=loop_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            conversation: List[Dict[str, Any]] = [
                {"role": "system", "content": "你是企业行政智能体。"},
                {"role": "user", "content": "检查香港天气并确认台风天办公与补贴政策。"},
            ]

            # --- Agentic Turn 1 ---
            r1 = client.post(
                "/v1/chat/completions",
                json={"model": "4argon", "messages": conversation, "tools": SAMPLE_TOOLS},
            )
            assert r1.status_code == 200
            msg1 = r1.json()["choices"][0]["message"]
            assert len(msg1["tool_calls"]) == 2
            conversation.append(msg1)
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": msg1["tool_calls"][0]["id"],
                    "content": '{"temp_c": 28, "warning": "Typhoon Signal No. 8"}',
                }
            )
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": msg1["tool_calls"][1]["id"],
                    "content": '{"rule": "Mandatory WFH during Signal No. 8; check wfh_allowance"}',
                }
            )

            # --- Agentic Turn 2 ---
            r2 = client.post(
                "/v1/chat/completions",
                json={"model": "4argon", "messages": conversation, "tools": SAMPLE_TOOLS},
            )
            assert r2.status_code == 200
            msg2 = r2.json()["choices"][0]["message"]
            assert len(msg2["tool_calls"]) == 1
            assert msg2["tool_calls"][0]["function"]["name"] == "search_policy"
            conversation.append(msg2)
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": msg2["tool_calls"][0]["id"],
                    "content": '{"allowance_hkd": 150}',
                }
            )

            # --- Agentic Turn 3 ---
            r3 = client.post(
                "/v1/chat/completions",
                json={"model": "4argon", "messages": conversation, "tools": SAMPLE_TOOLS},
            )
            assert r3.status_code == 200
            choice3 = r3.json()["choices"][0]
            assert choice3["finish_reason"] == "stop"
            assert "八号风球" in choice3["message"]["content"]

            th.join(timeout=3.0)

    assert len(browser_commands) == 3
    # Turn 1 starts a clean web session (`reset_chat=True`), while agentic steps 2 & 3 stay in-session (`False`)
    assert [c["reset_chat"] for c in browser_commands] == [True, False, False]
    assert [c["target_model"] for c in browser_commands] == ["4-argon", "4-argon", "4-argon"]
    # Step 2 & Step 3 used smart_incremental_delta (sent [TOOL_RESULT] without re-dumping full [Tool Calling Protocol])
    assert "[Tool Calling Protocol]" in browser_commands[0]["prompt"]
    assert "[Tool Calling Protocol]" not in browser_commands[1]["prompt"]
    assert "Typhoon Signal No. 8" in browser_commands[1]["prompt"]
    assert 'name="get_weather"' in browser_commands[1]["prompt"]
    assert "[Tool Calling Protocol]" not in browser_commands[2]["prompt"]
    assert '"allowance_hkd": 150' in browser_commands[2]["prompt"]


def test_response_format_json_object_sync_and_stream() -> None:
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-json-mode") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 21,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home",
                    "ready": True,
                }
            )
            ws.receive_json()

            def json_worker() -> None:
                ready.set()
                for _ in range(2):
                    cmd = ws.receive_json()
                    assert "[Output Format Requirement]" in cmd["prompt"]
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "reply": '```json\n{"status": "approved", "score": 98}\n```',
                            "citations": [{"title": "Ref", "url": "https://intranet.local/doc"}],
                        }
                    )

            th = threading.Thread(target=json_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            r_sync = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.8-flash",
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": "Return approval JSON"}],
                },
            )
            assert r_sync.status_code == 200
            parsed_sync = json.loads(r_sync.json()["choices"][0]["message"]["content"])
            assert parsed_sync == {"status": "approved", "score": 98}

            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.8-flash",
                    "stream": True,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": "Return approval JSON stream"}],
                },
            ) as r_stream:
                assert r_stream.status_code == 200
                out = ""
                for ln in r_stream.iter_lines():
                    if ln and ln != "data: [DONE]":
                        ch = json.loads(ln[6:])["choices"]
                        if ch and ch[0]["delta"].get("content"):
                            out += ch[0]["delta"]["content"]
                assert json.loads(out) == {"status": "approved", "score": 98}

            th.join(timeout=3.0)


def test_page_driver_shadow_dom_model_selection_and_new_chat() -> None:
    """Execute real `page-driver.js` in Node.js against a multi-layer Shadow DOM simulation."""
    import shutil
    import subprocess

    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to run Shadow DOM page-driver tests"

    script_path = Path(__file__).resolve().parent / "test_page_driver_dom.mjs"
    proc = subprocess.run(
        [node_bin, str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"DOM test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "ALL_SHADOW_DOM_DRIVER_TESTS_PASSED" in proc.stdout


def test_silent_background_tab_execution_roundtrip() -> None:
    """Verify silent prompt dispatch & streaming when the attached Chrome tab is in background (`hidden=True`)."""
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-bg-silent") as ws:
            ws.receive_json()
            # Report tab in background (`hidden: True`, `visibility_state: "hidden"`)
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 77,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/bg1",
                    "tab_title": "Gemini Enterprise (Background Tab)",
                    "ready": True,
                    "driver_mode": "auto",
                    "active_model": "Auto",
                }
            )
            ws.receive_json()

            def bg_worker() -> None:
                ready.set()
                cmd = ws.receive_json()
                assert cmd["type"] == "execute_prompt"
                assert cmd["target_model"] == "4-argon"
                assert cmd["reset_chat"] is True
                req_id = cmd["request_id"]
                ws.send_json({"type": "request_ack", "request_id": req_id})
                ws.send_json({"type": "stream_chunk", "request_id": req_id, "delta": "后台静默"})
                ws.send_json({"type": "stream_chunk", "request_id": req_id, "delta": "执行成功！"})
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": req_id,
                        "reply": "后台静默执行成功！",
                        "active_model": "4 Argon",
                        "source": "network_stream",
                    }
                )

            th = threading.Thread(target=bg_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "4argon",
                    "stream": True,
                    "messages": [{"role": "user", "content": "测试后台标签页静默发送"}],
                },
            ) as resp:
                assert resp.status_code == 200
                text_out = ""
                for ln in resp.iter_lines():
                    if ln and ln != "data: [DONE]":
                        choices = json.loads(ln[6:]).get("choices", [])
                        if choices and choices[0]["delta"].get("content"):
                            text_out += choices[0]["delta"]["content"]
                assert text_out == "后台静默执行成功！"

            th.join(timeout=3.0)


def test_large_120kb_tool_call_chunking_and_async_callback_stress() -> None:
    """Stress-test 125KB+ tool result outbound chunking, HTTP `/api/bridge/callback`, and <=16KB SSE slicing."""
    app = create_app(_make_test_settings())
    large_tool_result = "LINE_DATA_0123456789_ABCDEF\n" * 4800  # ~134 KB
    large_generated_code = "export const row = '0123456789abcdef';\n" * 3600  # ~140 KB

    write_tool = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write complete file content to disk",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        }
    ]

    reassembled_prompts: List[str] = []
    chunk_counts: List[int] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-120kb") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 88,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/stress",
                    "ready": True,
                }
            )
            ws.receive_json()

            def stress_worker() -> None:
                ready.set()
                for _ in range(2):
                    parts: Dict[int, str] = {}
                    while True:
                        packet = ws.receive_json()
                        if packet["type"] == "execute_prompt_chunk":
                            parts[packet["chunk_index"]] = packet["data"]
                        elif packet["type"] == "execute_prompt":
                            assert packet.get("chunked") is True
                            total = packet["total_chunks"]
                            chunk_counts.append(total)
                            merged_prompt = "".join(parts[i] for i in range(total))
                            reassembled_prompts.append(merged_prompt)

                            # Upload 140KB+ `[TOOL_CALL]` reply via async HTTP callback endpoint!
                            big_reply = json.dumps(
                                {
                                    "name": "write_file",
                                    "arguments": {
                                        "path": "src/generated_output.ts",
                                        "content": large_generated_code,
                                    },
                                },
                                ensure_ascii=False,
                            )
                            cb_res = client.post(
                                "/api/bridge/callback",
                                json={
                                    "client_id": "ext-120kb",
                                    "request_id": packet["request_id"],
                                    "reply": f"[TOOL_CALL]{big_reply}[/TOOL_CALL]",
                                    "source": "http_callback",
                                    "active_model": "4 Argon",
                                },
                            )
                            assert cb_res.status_code == 200
                            break

            th = threading.Thread(target=stress_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            messages_with_134kb_result = [
                {"role": "user", "content": "Read the huge log and generate output"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read_big",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"in.log","content":""}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_read_big",
                    "content": large_tool_result,
                },
            ]

            # 1) Non-streaming 134KB in -> 140KB tool_call out
            r_sync = client.post(
                "/v1/chat/completions",
                json={
                    "model": "4argon",
                    "stream": False,
                    "tools": write_tool,
                    "messages": messages_with_134kb_result,
                },
            )
            assert r_sync.status_code == 200
            tc_sync = r_sync.json()["choices"][0]["message"]["tool_calls"][0]
            assert tc_sync["function"]["name"] == "write_file"
            parsed_sync_args = json.loads(tc_sync["function"]["arguments"])
            assert parsed_sync_args["content"] == large_generated_code

            # 2) SSE Streaming (`stream=True`) 134KB in -> 140KB tool_call out across <=16KB SSE lines!
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "4argon-newchat",
                    "stream": True,
                    "tools": write_tool,
                    "messages": messages_with_134kb_result,
                },
            ) as r_stream:
                assert r_stream.status_code == 200
                sse_lines = [ln for ln in r_stream.iter_lines() if ln]

            th.join(timeout=5.0)

    assert len(chunk_counts) == 2
    assert all(cnt >= 5 for cnt in chunk_counts)
    assert all(large_tool_result in p for p in reassembled_prompts)

    # Verify NO single SSE line exceeded 20KB (immune to 64KB StreamReader line limits!)
    assert max(len(ln) for ln in sse_lines) < 20000
    assert sse_lines[-1] == "data: [DONE]"

    arg_fragments: List[str] = []
    fn_name_seen = ""
    finish_seen = ""
    for ln in sse_lines[:-1]:
        obj = json.loads(ln[6:])
        for ch in obj.get("choices", []):
            delta = ch.get("delta", {})
            for tc_delta in delta.get("tool_calls", []):
                fn_part = tc_delta.get("function", {})
                if fn_part.get("name"):
                    fn_name_seen = fn_part["name"]
                if fn_part.get("arguments"):
                    arg_fragments.append(fn_part["arguments"])
            if ch.get("finish_reason"):
                finish_seen = ch["finish_reason"]

    assert len(arg_fragments) >= 10, "140KB tool arguments must stream across multiple safe SSE chunks"
    assert fn_name_seen == "write_file"
    assert finish_seen == "tool_calls"
    reassembled_args = json.loads("".join(arg_fragments))
    assert reassembled_args["path"] == "src/generated_output.ts"
    assert reassembled_args["content"] == large_generated_code

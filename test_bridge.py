"""Automated pytest suite for Gemini Enterprise Web-to-API Bridge."""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
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
    parse_model_and_session,
    resolve_target_web_model,
)
from server import create_app


# ---------------------------------------------------------------------------
# 1. Schema, Context Builder, and Extension Icon Bundle Unit Tests
# ---------------------------------------------------------------------------


def test_extract_text_content_string_and_array() -> None:
    assert extract_text_content("你好 Gemini") == "你好 Gemini"
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    multimodal = [
        {"type": "text", "text": "第一段说明"},
        {"type": "input_text", "text": "第二段代码"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
    ]
    text = extract_text_content(multimodal)
    assert "第一段说明" in text
    assert "第二段代码" in text
    assert "Image attachment omitted" not in text

    msg = ChatMessage(role="user", content=multimodal)
    atts = msg.extract_attachments()
    assert len(atts) == 1
    assert atts[0].mime_type == "image/png"
    assert atts[0].media_kind == "image"


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
    tmp_dir = Path(tempfile.mkdtemp(prefix="geb_test_usage_"))
    defaults: Dict[str, Any] = {
        "auto_export_extension": False,
        "auto_update": False,
        "request_timeout_seconds": 10.0,
        "queue_timeout_seconds": 10.0,
        "usage_stats_path": str(tmp_dir / "usage_stats.json"),
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
    for alias in (
        "4carbon",
        "4-carbon",
        "carbon",
        "gemini4carbon",
        "gemini-4-carbon",
        "gemini-4.0-carbon",
        "gemini-4-carbon-newchat",
    ):
        assert resolve_target_web_model(alias) == "4-carbon"
    for alias in ("3.8flash", "3.8-flash", "gemini-3.8-flash", "gemini-3.8-flash-newchat"):
        assert resolve_target_web_model(alias) == "3.8-flash"
    assert resolve_target_web_model("2.5pro") == "2.5-pro"
    assert resolve_target_web_model("2.5flash") == "2.5-flash"
    assert resolve_target_web_model("gemini-web:Gemini 4.5 Ultra") == "Gemini 4.5 Ultra"
    assert resolve_target_web_model("gemini-4.2-pro") == "4.2-pro"

    assert model_requests_new_chat("gemini-4-argon-newchat") is True
    assert model_requests_new_chat("gemini-4-argon") is False
    assert model_requests_new_chat("gemini-4-carbon-newchat") is True
    assert model_requests_new_chat("gemini-4-carbon") is False

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
            "gemini-4-carbon",
            "gemini-4-carbon-newchat",
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
                for _ in range(4):
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

            for req_model in ("3.1pro", "gemini-4-argon", "gemini-4-carbon", "3.8flash-newchat"):
                r = client.post(
                    "/v1/chat/completions",
                    json={"model": req_model, "messages": [{"role": "user", "content": "Ping"}]},
                )
                assert r.status_code == 200

            t.join(timeout=3.0)
            status_data = client.get("/api/status").json()
            assert status_data["hub"]["active_client"]["active_model"] == "3.8-flash"

    assert [c["target_model"] for c in dispatched_commands] == ["3.1-pro", "4-argon", "4-carbon", "3.8-flash"]
    # With auto_reset_new_chat=True by default, each brand-new single-turn local conversation starts a clean web session
    assert [c["reset_chat"] for c in dispatched_commands] == [True, True, True, True]


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
        timeout=40,
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


# ---------------------------------------------------------------------------
# 6. Parallel Multi-Session Pool, Auto-Spawn & Sticky Affinity Tests
# ---------------------------------------------------------------------------


def test_parallel_sessions_auto_spawn_and_true_overlap() -> None:
    """Start with 1 tab; fire 3 concurrent sessions -> auto-spawns 2 tabs and runs all 3 in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    app = create_app(_make_test_settings(max_parallel_tabs=4, auto_spawn_tabs=True))
    spawned_tab_ids: List[int] = []
    executed_on_tabs: List[int] = []
    active_concurrency = 0
    peak_concurrency = 0
    conc_lock = threading.Lock()

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-parallel") as ws:
            ws.receive_json()  # welcome
            ws.send_json(
                {
                    "type": "tab_status",
                    "capabilities": ["multi_tab", "spawn_tab"],
                    "max_parallel_tabs": 4,
                    "tabs": [
                        {
                            "tab_id": 101,
                            "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid",
                            "tab_title": "Gemini Enterprise #101",
                            "ready": True,
                        }
                    ],
                }
            )
            ws.receive_json()  # ack

            next_tab_id = 102
            done_count = 0
            ws_send_lock = threading.Lock()

            def safe_ws_send(payload: Dict[str, Any]) -> None:
                with ws_send_lock:
                    ws.send_json(payload)

            def simulate_tab_work(cmd: Dict[str, Any]) -> None:
                nonlocal active_concurrency, peak_concurrency
                with conc_lock:
                    active_concurrency += 1
                    peak_concurrency = max(peak_concurrency, active_concurrency)
                try:
                    # Hold all 3 tabs concurrently for 180ms to prove true overlapping execution!
                    time.sleep(0.18)
                    safe_ws_send(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "tab_id": cmd["tab_id"],
                            "reply": f"Tab-{cmd['tab_id']}->{cmd['prompt']}",
                            "active_model": cmd.get("target_model") or "4 Argon",
                        }
                    )
                finally:
                    with conc_lock:
                        active_concurrency -= 1

            def extension_router() -> None:
                nonlocal next_tab_id, done_count
                ready.set()
                sub_threads: List[threading.Thread] = []
                while done_count < 3:
                    msg = ws.receive_json()
                    if msg["type"] == "spawn_tab":
                        new_tid = next_tab_id
                        next_tab_id += 1
                        spawned_tab_ids.append(new_tid)
                        safe_ws_send(
                            {
                                "type": "spawn_tab_result",
                                "spawn_id": msg["spawn_id"],
                                "ok": True,
                                "tab": {
                                    "tab_id": new_tid,
                                    "tab_url": msg.get("base_url") or "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid",
                                    "tab_title": f"Gemini Enterprise #{new_tid}",
                                    "ready": True,
                                },
                            }
                        )
                    elif msg["type"] == "execute_prompt":
                        done_count += 1
                        executed_on_tabs.append(msg["tab_id"])
                        t = threading.Thread(target=simulate_tab_work, args=(msg,), daemon=True)
                        t.start()
                        sub_threads.append(t)
                for t in sub_threads:
                    t.join(timeout=3.0)

            router_thread = threading.Thread(target=extension_router, daemon=True)
            router_thread.start()
            ready.wait(timeout=3.0)

            def call_session(idx: int) -> httpx.Response:
                return client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "4argon",
                        "messages": [{"role": "user", "content": f"ParallelQuery-{idx}"}],
                    },
                )

            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(call_session, i) for i in (1, 2, 3)]
                responses = [f.result(timeout=8.0) for f in futures]
            elapsed = time.monotonic() - t0

            router_thread.join(timeout=3.0)

            status = client.get("/api/status").json()
            assert status["hub"]["parallel_capacity"] == 3

    assert all(r.status_code == 200 for r in responses)
    # Must have auto-spawned 2 new background tabs (#102 and #103) so all 3 tabs were distinct!
    assert sorted(spawned_tab_ids) == [102, 103]
    assert sorted(executed_on_tabs) == [101, 102, 103]
    # Peak concurrency >= 2 and total wall time well below sequential 3 * 0.18 = 0.54s!
    assert peak_concurrency >= 2
    assert elapsed < 0.48


def test_parallel_interleaved_multi_turn_and_named_session_affinity() -> None:
    """Verify two parallel multi-turn sessions stick to their own tabs without resetting each other."""
    assert parse_model_and_session("gemini-4-argon@alpha") == ("gemini-4-argon", "alpha")
    assert parse_model_and_session("3.1pro#beta") == ("3.1pro", "beta")
    assert parse_model_and_session("4argon@alpha-newchat") == ("4argon-newchat", "alpha")
    assert resolve_target_web_model("gemini-4-argon@alpha") == "4-argon"
    assert model_requests_new_chat("4argon@alpha-newchat") is True

    app = create_app(_make_test_settings(max_parallel_tabs=4, auto_spawn_tabs=True))
    tab_history_log: Dict[int, List[Dict[str, Any]]] = {201: [], 202: []}

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-sticky") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "capabilities": ["multi_tab", "spawn_tab"],
                    "tabs": [
                        {
                            "tab_id": 201,
                            "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid",
                            "ready": True,
                        },
                        {
                            "tab_id": 202,
                            "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid",
                            "ready": True,
                        },
                    ],
                }
            )
            ws.receive_json()

            def sticky_worker() -> None:
                ready.set()
                for idx in range(5):
                    cmd = ws.receive_json()
                    assert cmd["type"] == "execute_prompt"
                    tid = cmd["tab_id"]
                    tab_history_log[tid].append(cmd)
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "tab_id": tid,
                            "reply": f"Ans-{idx + 1}-from-{tid}",
                            "active_model": cmd.get("target_model") or "default",
                        }
                    )

            th = threading.Thread(target=sticky_worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            # Session Alpha Turn 1 -> claims one tab (e.g. #201)
            alpha_msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Alpha Turn 1"}]
            r_a1 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-argon@alpha", "messages": alpha_msgs},
            )
            assert r_a1.status_code == 200
            alpha_slot = r_a1.headers["x-bridge-slot"]
            alpha_msgs.append({"role": "assistant", "content": r_a1.json()["choices"][0]["message"]["content"]})

            # Session Beta Turn 1 -> claims the OTHER tab (#202) without touching Alpha's tab!
            beta_msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Beta Turn 1"}]
            r_b1 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-3.8-flash", "messages": beta_msgs},
            )
            assert r_b1.status_code == 200
            beta_slot = r_b1.headers["x-bridge-slot"]
            assert alpha_slot != beta_slot
            beta_msgs.append({"role": "assistant", "content": r_b1.json()["choices"][0]["message"]["content"]})

            # Session Alpha Turn 2 -> returns to `alpha_slot` with `reset_chat=False`!
            alpha_msgs.append({"role": "user", "content": "Alpha Turn 2"})
            r_a2 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-argon@alpha", "messages": alpha_msgs},
            )
            assert r_a2.status_code == 200
            assert r_a2.headers["x-bridge-slot"] == alpha_slot

            # Session Beta Turn 2 (even without `@tag`, matched via message-history prefix!) -> returns to `beta_slot` with `reset_chat=False`!
            beta_msgs.append({"role": "user", "content": "Beta Turn 2"})
            r_b2 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-3.8-flash", "messages": beta_msgs},
            )
            assert r_b2.status_code == 200
            assert r_b2.headers["x-bridge-slot"] == beta_slot

            # Session Alpha starts a brand-new topic using `@alpha` -> resets ONLY `alpha_slot`, leaving `beta_slot` untouched!
            r_a_new = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-4-argon@alpha",
                    "messages": [{"role": "user", "content": "Alpha Fresh Topic"}],
                },
            )
            assert r_a_new.status_code == 200
            assert r_a_new.headers["x-bridge-slot"] == alpha_slot

            th.join(timeout=3.0)

    alpha_tid = int(alpha_slot.split(":")[1])
    beta_tid = int(beta_slot.split(":")[1])
    assert [c["reset_chat"] for c in tab_history_log[alpha_tid]] == [True, False, True]
    assert tab_history_log[alpha_tid][1]["prompt"] == "Alpha Turn 2"
    assert [c["reset_chat"] for c in tab_history_log[beta_tid]] == [True, False]
    assert tab_history_log[beta_tid][1]["prompt"] == "Beta Turn 2"


# ---------------------------------------------------------------------------
# 7. Regression Tests: Deduplicated Carbon, Sliding Timeout, Ephemeral Metadata & Compaction
# ---------------------------------------------------------------------------


def test_gemini_4_carbon_deduplicated_and_context_window_metadata() -> None:
    """Ensure gemini-4-carbon appears once in /v1/models and carries 1M context window metadata."""
    from schemas import AVAILABLE_MODELS, estimate_tokens

    ids = [m["id"] for m in AVAILABLE_MODELS]
    assert ids.count("gemini-4-carbon") == 1
    assert ids.count("gemini-4-carbon-newchat") == 1
    assert len(ids) == len(set(ids)), "AVAILABLE_MODELS must not contain duplicate model IDs"

    carbon_entry = next(m for m in AVAILABLE_MODELS if m["id"] == "gemini-4-carbon")
    assert carbon_entry["context_length"] == 1048576
    assert carbon_entry["context_window"] == 1048576
    assert carbon_entry["max_prompt_tokens"] == 1048576
    assert carbon_entry["max_input_tokens"] == 1048576
    assert carbon_entry["input_token_limit"] == 1048576
    assert carbon_entry["max_output_tokens"] == 65536
    assert carbon_entry["max_completion_tokens"] == 65536
    assert carbon_entry["output_token_limit"] == 65536
    assert carbon_entry["max_tokens"] == 1048576
    assert carbon_entry["limits"] == {
        "max_prompt_tokens": 1048576,
        "max_output_tokens": 65536,
        "max_context_window_tokens": 1048576,
    }

    # Verify estimate_tokens does not 3x-inflate JSON punctuation
    sample_json = json.dumps({"key": "value", "arr": [1, 2, 3, 4, 5], "nested": {"a": "hello world"}}) * 100
    tokens = estimate_tokens(sample_json)
    # ~7800 chars of JSON should be ~1800-2200 tokens, never >4000 tokens
    assert 1200 <= tokens <= 2600

    # Tool instructions and schema constraints must survive large tool catalogs.
    from tool_calling import build_tool_system_instructions

    many_tools = [
        {
            "type": "function",
            "function": {
                "name": f"mcp_tool_{i}",
                "description": ("Very long tool description " * 40),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "arg1": {"type": "string", "description": ("Verbose property docs " * 30), "enum": ["a", "b"]}
                    },
                    "required": ["arg1"],
                },
            },
        }
        for i in range(20)
    ]
    rendered_tools = build_tool_system_instructions(many_tools)
    assert many_tools[0]["function"]["description"] in rendered_tools
    assert many_tools[0]["function"]["parameters"]["properties"]["arg1"]["description"] in rendered_tools
    assert '"enum":["a","b"]' in rendered_tools
    assert '"required":["arg1"]' in rendered_tools


def test_ephemeral_per_turn_metadata_preserves_smart_incremental_delta() -> None:
    """Verify volatile <system_reminder>, <current_datetime>, <canvas-context> tags never break smart_incremental_delta."""
    from context_builder import ConversationContextBuilder
    from schemas import ChatMessage

    tracker = ConversationContextBuilder()
    # Turn 1: Copilot injects <current_datetime> and <system_reminder> on User message 1
    t1_msgs = [
        ChatMessage(role="system", content="You are a coding assistant.\n<system_reminder> dynamic skills v1 </system_reminder>"),
        ChatMessage(
            role="user",
            content=(
                "<current_datetime>2026-11-12T14:00:00+08:00</current_datetime>\n"
                "Fix the timeout bug in page-driver.js\n"
                "<system_reminder>Deferred tools list...</system_reminder>"
            ),
        ),
    ]
    prep1 = tracker.build(t1_msgs, model="gemini-4-carbon", session_key="slot-1")
    assert prep1.strategy_used == "smart_single_turn"
    assert prep1.reset_chat is True
    tracker.record_completed_turn(
        "slot-1",
        prep1.normalized_history,
        assistant_reply="I will inspect page-driver.js.",
        active_model="4 Carbon",
        tools_fingerprint=prep1.tools_fingerprint,
        system_fingerprint=prep1.system_fingerprint,
    )

    # Turn 2: Client strips <system_reminder> from historical User message 1 and adds a new <current_datetime> to User message 2!
    t2_msgs = [
        ChatMessage(role="system", content="You are a coding assistant.\n<system_reminder> dynamic skills v2 changed </system_reminder>"),
        ChatMessage(role="user", content="Fix the timeout bug in page-driver.js"),
        ChatMessage(role="assistant", content="I will inspect page-driver.js."),
        ChatMessage(
            role="user",
            content="<current_datetime>2026-11-12T14:02:00+08:00</current_datetime>\nNow please run the tests.",
        ),
    ]
    prep2 = tracker.build(t2_msgs, model="gemini-4-carbon", session_key="slot-1")
    assert prep2.strategy_used == "smart_incremental_delta", f"Expected smart_incremental_delta, got {prep2.strategy_used}"
    assert prep2.reset_chat is False
    assert "Now please run the tests." in prep2.prompt
    assert "Fix the timeout bug in page-driver.js" not in prep2.prompt


def test_sliding_activity_timeout_and_sse_keepalive_and_aux_compaction_isolation() -> None:
    """Verify request_progress slides deadline past initial timeout and auxiliary compaction calls don't wipe tool sessions."""
    app = create_app(_make_test_settings(request_timeout_seconds=0.35, max_parallel_tabs=4, auto_spawn_tabs=True))
    dispatched_slots: List[int] = []
    dispatched_resets: List[bool] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-sliding") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "tab_status",
                    "capabilities": ["multi_tab", "spawn_tab"],
                    "tabs": [
                        {"tab_id": 301, "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/c1", "ready": True},
                        {"tab_id": 302, "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/c1", "ready": True},
                    ],
                }
            )
            ws.receive_json()

            def worker() -> None:
                ready.set()
                # Request 1: Main tool-enabled turn on Tab 301, takes 0.55s (> 0.35s timeout!) but sends request_progress every 0.15s
                c1 = ws.receive_json()
                dispatched_slots.append(c1["tab_id"])
                dispatched_resets.append(c1["reset_chat"])
                for _ in range(3):
                    time.sleep(0.16)
                    ws.send_json(
                        {
                            "type": "request_progress",
                            "request_id": c1["request_id"],
                            "tab_id": c1["tab_id"],
                            "generating": True,
                            "dom_len": 42,
                        }
                    )
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c1["request_id"],
                        "tab_id": c1["tab_id"],
                        "reply": "Deep thinking completed without timeout!",
                    }
                )

                # Request 2: Auxiliary compaction/summarization call (`tools` omitted!) using same `@main` session tag
                # Must be routed away from Tab 301 to Tab 302 so Tab 301's tool session is NOT wiped!
                c2 = ws.receive_json()
                dispatched_slots.append(c2["tab_id"])
                dispatched_resets.append(c2["reset_chat"])
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c2["request_id"],
                        "tab_id": c2["tab_id"],
                        "reply": "Summary of conversation",
                    }
                )

                # Request 3: Turn 2 of Main tool-enabled session on `@main` -> still on Tab 301 with `reset_chat=False`!
                c3 = ws.receive_json()
                dispatched_slots.append(c3["tab_id"])
                dispatched_resets.append(c3["reset_chat"])
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c3["request_id"],
                        "tab_id": c3["tab_id"],
                        "reply": "Turn 2 incremental reply!",
                    }
                )

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            # 1) Turn 1 with tools on `@main` (exceeds 0.35s initial timeout, kept alive by request_progress)
            msgs = [{"role": "user", "content": "Start main agentic task"}]
            r1 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-carbon@main", "messages": msgs, "tools": SAMPLE_TOOLS},
            )
            assert r1.status_code == 200
            assert r1.json()["choices"][0]["message"]["content"] == "Deep thinking completed without timeout!"
            msgs.append({"role": "assistant", "content": "Deep thinking completed without timeout!"})

            # 2) Auxiliary compaction request WITHOUT tools on `@main`
            r_aux = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-4-carbon@main",
                    "messages": [{"role": "user", "content": "Summarize the conversation so far in 3 bullets."}],
                },
            )
            assert r_aux.status_code == 200

            # 3) Turn 2 with tools on `@main` -> must still match Tab 301 incrementally (`reset_chat=False`)!
            msgs.append({"role": "user", "content": "Continue main agentic task step 2"})
            r3 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-carbon@main", "messages": msgs, "tools": SAMPLE_TOOLS},
            )
            assert r3.status_code == 200
            assert r3.json()["choices"][0]["message"]["content"] == "Turn 2 incremental reply!"

            th.join(timeout=3.0)

    assert dispatched_slots == [301, 302, 301], f"Aux compaction must not evict main tool tab: {dispatched_slots}"
    assert dispatched_resets == [True, True, False], f"Main tool tab must stay incremental on Turn 2: {dispatched_resets}"


# ---------------------------------------------------------------------------
# 7. Official Pricing Engine, Persistent Usage Ledger & Live Context Monitor
# ---------------------------------------------------------------------------


def test_usage_tracker_official_pricing_persistence_and_reset(tmp_path: Path) -> None:
    from usage_stats import UsageTracker

    ledger_file = tmp_path / "usage_stats.json"
    tracker = UsageTracker(storage_path=ledger_file)

    # 1) Gemini 4 Carbon intro rate (2026-11-15): $2.00 input, $0.10 cached, $10.00 output
    rec_c1 = tracker.record_request(
        requested_model="gemini-4-carbon",
        prompt_tokens=100_000,
        completion_tokens=10_000,
        strategy="smart_incremental_delta",
        cached_tokens=80_000,
        delta_prompt_tokens=20_000,
        as_of_date="2026-11-15",
    )
    # Uncached: 0.1M * $2.00 ($0.20) + 0.01M * $10.00 ($0.10) = $0.30
    assert abs(rec_c1["equivalent_cost_usd"] - 0.30) < 1e-6
    # Cached: 0.08M * $0.10 ($0.008) + 0.02M * $2.00 ($0.04) + 0.01M * $10.00 ($0.10) = $0.148
    assert abs(rec_c1["cached_equivalent_cost_usd"] - 0.148) < 1e-6
    assert abs(rec_c1["equivalent_cost_cny"] - round(0.30 * 6.8, 4)) < 1e-4

    # 2) Gemini 4 Carbon post-intro rate (2026-12-05): $4.00 input, $0.20 cached, $20.00 output
    rec_c2 = tracker.record_request(
        requested_model="4carbon",
        prompt_tokens=100_000,
        completion_tokens=10_000,
        strategy="smart_single_turn",
        as_of_date="2026-12-05",
    )
    assert abs(rec_c2["equivalent_cost_usd"] - 0.60) < 1e-6

    # 3) Gemini 3.1 Pro tiered pricing: <=200k ($2/$12) vs >200k ($4/$18)
    rec_p_low = tracker.record_request(
        requested_model="gemini-3.1-pro",
        prompt_tokens=150_000,
        completion_tokens=10_000,
        as_of_date="2026-11-15",
    )
    assert abs(rec_p_low["equivalent_cost_usd"] - (0.15 * 2.0 + 0.01 * 12.0)) < 1e-6

    rec_p_high = tracker.record_request(
        requested_model="gemini-3.1-pro",
        prompt_tokens=250_000,
        completion_tokens=10_000,
        as_of_date="2026-11-15",
    )
    assert abs(rec_p_high["equivalent_cost_usd"] - (0.25 * 4.0 + 0.01 * 18.0)) < 1e-6

    # 4) Gemini 3.8 Flash intro rate ($0.75 / $3.75) + Auto resolved from browser slot model
    rec_flash = tracker.record_request(
        requested_model="gemini-enterprise-web",
        browser_active_model="3.8 Flash",
        prompt_tokens=200_000,
        completion_tokens=20_000,
        as_of_date="2026-11-15",
    )
    assert rec_flash["model"] == "gemini-3.8-flash"
    assert abs(rec_flash["equivalent_cost_usd"] - (0.2 * 0.75 + 0.02 * 3.75)) < 1e-6

    assert ledger_file.is_file()

    # Reload from disk into a fresh UsageTracker instance
    reloaded = UsageTracker(storage_path=ledger_file)
    snap = reloaded.snapshot(active_model_hint="4 Carbon", as_of_date="2026-11-15", include_catalog=True)
    assert abs(snap["usd_to_cny_rate"] - 6.8) < 1e-6
    assert snap["all_time"]["requests"] == 5
    assert snap["current_session"]["requests"] == 0
    assert "gemini-4-carbon" in snap["by_model"]
    assert "gemini-3.1-pro" in snap["by_model"]
    assert "gemini-3.8-flash" in snap["by_model"]
    assert len(snap["pricing_catalog"]) >= 6

    # Scoped reset: today vs all
    reloaded.reset(scope="today", as_of_date="2026-11-15")
    assert reloaded.snapshot(as_of_date="2026-11-15")["today"]["requests"] == 0
    assert reloaded.snapshot(as_of_date="2026-11-15")["all_time"]["requests"] == 5

    reloaded.reset(scope="all", as_of_date="2026-11-15")
    assert reloaded.snapshot(as_of_date="2026-11-15")["all_time"]["requests"] == 0
    assert reloaded.snapshot(as_of_date="2026-11-15")["by_model"] == {}


def test_e2e_context_monitor_and_usage_api_sync_and_sse(tmp_path: Path) -> None:
    ledger_path = tmp_path / "e2e_usage.json"
    app = create_app(_make_test_settings(usage_stats_path=str(ledger_path)))
    ws_updates: List[Dict[str, Any]] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-telemetry") as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            assert "usage_snapshot" in welcome

            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 77,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/ctx1",
                    "tab_title": "Gemini Enterprise",
                    "active_model": "4 Carbon",
                    "capabilities": ["multi_tab", "usage_push"],
                    "ready": True,
                }
            )
            ack = ws.receive_json()
            assert ack["type"] == "ack"
            assert "usage_snapshot" in ack

            def recv_next_command() -> Dict[str, Any]:
                while True:
                    msg = ws.receive_json()
                    if msg.get("type") == "usage_update":
                        ws_updates.append(msg)
                        continue
                    return msg

            def worker() -> None:
                ready.set()
                # Turn 1 (non-streaming)
                c1 = recv_next_command()
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c1["request_id"],
                        "tab_id": 77,
                        "active_model": "4 Carbon",
                        "reply": "First turn architectural response with detailed analysis.",
                    }
                )
                # Turn 2 (streaming SSE incremental delta)
                c2 = recv_next_command()
                assert c2["reset_chat"] is False
                for delta in ("Second turn ", "streaming follow-up ", "complete."):
                    ws.send_json({"type": "stream_chunk", "request_id": c2["request_id"], "delta": delta})
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": c2["request_id"],
                        "tab_id": 77,
                        "active_model": "4 Carbon",
                        "reply": "Second turn streaming follow-up complete.",
                    }
                )

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            ready.wait(timeout=3.0)

            history: List[Dict[str, Any]] = [
                {"role": "system", "content": "You are a principal distributed systems architect."},
                {"role": "user", "content": "Design a multi-tab browser worker pool with sticky session affinity."},
            ]
            r1 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-carbon", "messages": history, "tools": SAMPLE_TOOLS},
            )
            assert r1.status_code == 200
            history.append({"role": "assistant", "content": r1.json()["choices"][0]["message"]["content"]})

            history.append({"role": "user", "content": "Now add real-time context window monitoring."})
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "gemini-4-carbon", "stream": True, "messages": history, "tools": SAMPLE_TOOLS},
            ) as stream_resp:
                assert stream_resp.status_code == 200
                sse_lines = [ln for ln in stream_resp.iter_lines() if ln]
            assert sse_lines[-1] == "data: [DONE]"

            th.join(timeout=3.0)

            status_data = client.get("/api/status").json()
            usage_data = client.get("/api/usage").json()

            assert status_data["usage"]["all_time"]["requests"] == 2
            assert status_data["usage"]["today"]["requests"] == 2
            assert status_data["usage"]["all_time"]["equivalent_cost_usd"] > 0
            assert (
                status_data["usage"]["all_time"]["cached_equivalent_cost_usd"]
                < status_data["usage"]["all_time"]["equivalent_cost_usd"]
            )
            assert "gemini-4-carbon" in usage_data["usage"]["by_model"]
            assert "pricing_catalog" in usage_data["usage"]

            ctx_mon = status_data["context_monitor"]
            assert ctx_mon["active_sessions"] == 1
            prim = ctx_mon["primary"]
            assert prim["tab_id"] == 77
            assert prim["turn_count"] == 4  # 2 user + 2 assistant
            assert prim["user_turns"] == 2
            assert prim["total_context_tokens"] > prim["history_tokens"] > 0
            assert prim["tools_tokens"] > 0
            assert prim["context_limit_tokens"] == 1_048_576
            assert prim["utilization_pct"] >= 0.0
            assert prim["last_strategy"] == "smart_incremental_delta"

            # Reset via POST /api/usage/reset
            reset_resp = client.post("/api/usage/reset", json={"scope": "all"})
            assert reset_resp.status_code == 200
            assert reset_resp.json()["usage"]["all_time"]["requests"] == 0


# ---------------------------------------------------------------------------
# 8. GitHub Releases Packaging, Auto-Updater, SHA-256 & Cross-Version Sync
# ---------------------------------------------------------------------------


def _populate_minimal_repo_tree(repo_dir: Path, version: str = "1.1.0", marker: str = "v1") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "version.py").write_text(
        f'APP_VERSION = "{version}"\nDEFAULT_GITHUB_REPO = "StevenLee1471717/Gemini-Enterprise-Bridge"\n',
        encoding="utf-8",
    )
    (repo_dir / "server.py").write_text(f"# server {marker} v{version}\n", encoding="utf-8")
    (repo_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Gemini Enterprise Bridge",
                "version": version,
                "action": {"default_popup": "popup.html"},
            }
        ),
        encoding="utf-8",
    )
    for fname in ("background.js", "content-bridge.js", "page-driver.js", "popup.html", "popup.css", "popup.js"):
        (repo_dir / fname).write_text(f"// {fname} {marker} v{version}\n", encoding="utf-8")
    (repo_dir / "gemini-enterprise-bridge.user.js").write_text(
        f"// ==UserScript==\n// @version {version}\n// ==/UserScript==\n// {marker}\n",
        encoding="utf-8",
    )


def test_semver_comparison_and_release_artifact_builder(tmp_path: Path) -> None:
    import zipfile
    from extension_exporter import build_release_artifacts
    from updater import compare_semver, normalize_version, sha256_file

    assert normalize_version("v1.2.0") == "1.2.0"
    assert compare_semver("1.1.0", "1.1.0") == 0
    assert compare_semver("v1.2.0", "1.1.9") == 1
    assert compare_semver("1.1.0", "1.2.0") == -1
    assert compare_semver("1.2.0-rc.1", "1.2.0") == -1
    assert compare_semver("2.0.0", "1.99.99") == 1

    src_repo = tmp_path / "src_repo"
    _populate_minimal_repo_tree(src_repo, version="1.2.0", marker="release120")
    # Sensitive user files MUST NEVER leak into backend release zip
    (src_repo / ".env").write_text("BRIDGE_API_KEY=super-secret-do-not-pack\n", encoding="utf-8")
    (src_repo / "usage_stats.json").write_text('{"secret_ledger": true}', encoding="utf-8")

    dist_dir = tmp_path / "dist_out"
    manifest = build_release_artifacts(
        dist_dir=dist_dir,
        root_dir=src_repo,
        version="1.2.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
    )

    assert manifest["version"] == "1.2.0"
    assert manifest["tag"] == "v1.2.0"
    ext_zip = dist_dir / "gemini-enterprise-bridge-extension.zip"
    backend_zip = dist_dir / "gemini-enterprise-bridge-backend.zip"
    userscript_out = dist_dir / "gemini-enterprise-bridge.user.js"
    update_xml = dist_dir / "update-manifest.xml"
    release_json = dist_dir / "release-manifest.json"

    for f in (ext_zip, backend_zip, userscript_out, update_xml, release_json):
        assert f.is_file() and f.stat().st_size > 0

    assert sha256_file(ext_zip) == manifest["extension_zip_sha256"]
    assert sha256_file(backend_zip) == manifest["backend_zip_sha256"]
    assert sha256_file(userscript_out) == manifest["userscript_sha256"]
    xml_text = update_xml.read_text(encoding="utf-8")
    assert "version='1.2.0'" in xml_text or 'version="1.2.0"' in xml_text

    with zipfile.ZipFile(backend_zip, "r") as zf:
        names = set(zf.namelist())
        assert "server.py" in names
        assert "extension/manifest.json" in names
        assert ".env" not in names
        assert "usage_stats.json" not in names

    with zipfile.ZipFile(ext_zip, "r") as ezf:
        ext_names = set(ezf.namelist())
        assert "manifest.json" in ext_names
        assert "background.js" in ext_names
        assert "icons/icon-128.png" in ext_names


def test_updater_apply_preserves_env_and_ledger_and_rejects_tampered_sha256(tmp_path: Path) -> None:
    from extension_exporter import build_release_artifacts
    from updater import UpdateIntegrityError, UpdateManager

    # Build a v1.2.0 release package in a staging directory
    upstream_repo = tmp_path / "upstream_v120"
    _populate_minimal_repo_tree(upstream_repo, version="1.2.0", marker="upgraded_from_github")
    dist_dir = tmp_path / "github_releases_dist"
    rel_manifest = build_release_artifacts(
        dist_dir=dist_dir,
        root_dir=upstream_repo,
        version="1.2.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
    )

    # Prepare local v1.1.0 installation with custom .env and usage_stats.json
    local_install = tmp_path / "local_v110"
    _populate_minimal_repo_tree(local_install, version="1.1.0", marker="old_local")
    env_bytes = b"BRIDGE_API_KEY=keep-my-local-secret\nBRIDGE_USD_TO_CNY_RATE=6.8\n"
    ledger_bytes = b'{"schema_version": 1, "all_time": {"requests": 99}}'
    (local_install / ".env").write_bytes(env_bytes)
    (local_install / "usage_stats.json").write_bytes(ledger_bytes)

    url_map: Dict[str, bytes] = {
        "https://github.com/StevenLee1471717/Gemini-Enterprise-Bridge/releases/latest/download/release-manifest.json": (
            dist_dir / "release-manifest.json"
        ).read_bytes(),
        rel_manifest["extension_zip_url"]: (dist_dir / "gemini-enterprise-bridge-extension.zip").read_bytes(),
        rel_manifest["backend_zip_url"]: (dist_dir / "gemini-enterprise-bridge-backend.zip").read_bytes(),
        rel_manifest["userscript_url"]: (dist_dir / "gemini-enterprise-bridge.user.js").read_bytes(),
    }

    def mock_fetcher(url: str, _timeout: float, _accept: str) -> bytes:
        if url not in url_map:
            raise RuntimeError(f"404 Not Found: {url}")
        return url_map[url]

    mgr = UpdateManager(
        root_dir=local_install,
        current_version="1.1.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
        url_fetcher=mock_fetcher,
    )
    chk = mgr.check_github_update(force=True)
    assert chk["ok"] is True
    assert chk["latest_github_version"] == "1.2.0"
    assert chk["backend_outdated"] is True
    assert chk["extension_outdated"] is True

    # 1) Reject tampered SHA-256 archive before touching any local file
    tampered_map = dict(url_map)
    tampered_map[rel_manifest["extension_zip_url"]] = b"corrupted-zip-payload"
    bad_mgr = UpdateManager(
        root_dir=local_install,
        current_version="1.1.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
        url_fetcher=lambda u, t, a: tampered_map[u],
    )
    try:
        bad_mgr.update_extension_only()
        raise AssertionError("Expected UpdateIntegrityError for tampered zip")
    except UpdateIntegrityError:
        pass

    # 2) Extension-only hot update
    ext_res = mgr.update_extension_only()
    assert ext_res["ok"] is True
    assert ext_res["updated_version"] == "1.2.0"
    ext_mf = json.loads((local_install / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert ext_mf["version"] == "1.2.0"
    assert "upgraded_from_github" in (local_install / "extension" / "background.js").read_text(encoding="utf-8")

    # 3) Full backend + extension update strictly preserves .env and usage_stats.json
    full_res = mgr.update_backend_and_extension(allow_git=False)
    assert full_res["ok"] is True
    assert full_res["strategy"] == "release_zip"
    assert full_res["updated_version"] == "1.2.0"
    assert "upgraded_from_github" in (local_install / "server.py").read_text(encoding="utf-8")
    assert (local_install / ".env").read_bytes() == env_bytes
    assert (local_install / "usage_stats.json").read_bytes() == ledger_bytes


def test_e2e_cross_version_ws_handshake_and_update_endpoints(tmp_path: Path) -> None:
    from extension_exporter import build_release_artifacts
    from updater import UpdateManager

    # Build mocked v1.3.0 GitHub release
    rel_src = tmp_path / "rel_v130"
    _populate_minimal_repo_tree(rel_src, version="1.3.0", marker="rel130")
    dist_dir = tmp_path / "dist_v130"
    rel_manifest = build_release_artifacts(
        dist_dir=dist_dir,
        root_dir=rel_src,
        version="1.3.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
    )
    url_map: Dict[str, bytes] = {
        "https://github.com/StevenLee1471717/Gemini-Enterprise-Bridge/releases/latest/download/release-manifest.json": (
            dist_dir / "release-manifest.json"
        ).read_bytes(),
        rel_manifest["extension_zip_url"]: (dist_dir / "gemini-enterprise-bridge-extension.zip").read_bytes(),
        rel_manifest["backend_zip_url"]: (dist_dir / "gemini-enterprise-bridge-backend.zip").read_bytes(),
        rel_manifest["userscript_url"]: (dist_dir / "gemini-enterprise-bridge.user.js").read_bytes(),
    }

    install_dir = tmp_path / "runtime_install"
    _populate_minimal_repo_tree(install_dir, version="1.1.0", marker="base110")
    (install_dir / ".env").write_text("BRIDGE_USD_TO_CNY_RATE=6.8\n", encoding="utf-8")

    # Case A: Extension already auto-updated to v1.3.0 while backend is on v1.1.0 -> prompts backend update!
    mgr_old_backend = UpdateManager(
        root_dir=install_dir,
        current_version="1.1.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
        url_fetcher=lambda u, _t, _a: url_map[u],
    )
    app_a = create_app(_make_test_settings(), update_manager=mgr_old_backend)
    with TestClient(app_a) as client:
        with client.websocket_connect("/ws/browser?client_id=ext-newer") as ws:
            welcome = ws.receive_json()
            assert welcome["backend_version"] == "1.1.0"
            ws.send_json(
                {
                    "type": "tab_status",
                    "extension_version": "1.3.0",
                    "capabilities": ["multi_tab", "usage_push", "auto_update"],
                    "tab_id": 11,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                    "ready": True,
                }
            )
            ack = ws.receive_json()
            assert ack["type"] == "ack"
            assert ack["backend_update_required"] is True

            st = client.get("/api/status").json()
            assert st["connected_extension_version"] == "1.3.0"
            assert st["backend_update_required"] is True
            assert st["update"]["backend_outdated"] is True

            upd_st = client.get("/api/update/status?force=true").json()
            assert upd_st["latest_github_version"] == "1.3.0"
            assert upd_st["backend_update_required"] is True

            # Apply update via HTTP endpoint (non-supervised test process -> no os._exit)
            apply_res = client.post("/api/update/apply", json={"restart": False}).json()
            assert apply_res["ok"] is True
            assert apply_res["updated_version"] == "1.3.0"
            reload_evt = ws.receive_json()
            assert reload_evt["type"] == "extension_reload_available"
            assert reload_evt["version"] == "1.3.0"
            assert "rel130" in (install_dir / "server.py").read_text(encoding="utf-8")
            assert "BRIDGE_USD_TO_CNY_RATE=6.8" in (install_dir / ".env").read_text(encoding="utf-8")

    # Case B: Backend auto-updated on startup to v1.3.0 while browser still runs v1.1.0 -> auto-triggers reload!
    mgr_new_backend = UpdateManager(
        root_dir=install_dir,
        current_version="1.3.0",
        repo="StevenLee1471717/Gemini-Enterprise-Bridge",
        url_fetcher=lambda u, _t, _a: url_map[u],
    )
    app_b = create_app(_make_test_settings(), update_manager=mgr_new_backend)
    with TestClient(app_b) as client:
        with client.websocket_connect("/ws/browser?client_id=ext-older") as ws:
            welcome_b = ws.receive_json()
            assert welcome_b["backend_version"] == "1.3.0"
            ws.send_json(
                {
                    "type": "tab_status",
                    "extension_version": "1.1.0",
                    "capabilities": ["multi_tab", "usage_push", "auto_update"],
                    "tab_id": 12,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                    "ready": True,
                }
            )
            ack_b = ws.receive_json()
            assert ack_b["type"] == "ack"
            assert ack_b["backend_version"] == "1.3.0"
            reload_msg = ws.receive_json()
            assert reload_msg == {"type": "extension_reload_available", "version": "1.3.0"}


# ---------------------------------------------------------------------------
# 3. Multimodal File & Media Normalization, Delta Tracking, Staging & GCloud
# ---------------------------------------------------------------------------


def _build_minimal_ooxml_zip(kind: str, text_snippet: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if kind == "docx":
            zf.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text_snippet}</w:t></w:r></w:p></w:body></w:document>",
            )
        elif kind == "xlsx":
            zf.writestr(
                "xl/sharedStrings.xml",
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<si><t>{text_snippet}</t></si></sst>",
            )
            zf.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row></sheetData></worksheet>',
            )
        elif kind == "pptx":
            zf.writestr(
                "ppt/slides/slide1.xml",
                f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text_snippet}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
    return buf.getvalue()


def test_multimodal_parsing_magic_sniffing_ooxml_and_local_files(tmp_path: Path) -> None:
    import base64
    from multimodal import extract_message_attachments

    png_bytes = render_spark_icon_png(16)
    wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
    mp4_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    pdf_bytes = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"
    docx_bytes = _build_minimal_ooxml_zip("docx", "季度营收增长 38%")
    xlsx_bytes = _build_minimal_ooxml_zip("xlsx", "预算科目A")
    pptx_bytes = _build_minimal_ooxml_zip("pptx", "产品路线图里程碑")

    local_md = tmp_path / "notes.md"
    local_md.write_text("# 架构评审纪要\n- 支持多模态直传", encoding="utf-8")

    content_parts = [
        {"type": "text", "text": "请综合分析以下所有多模态材料"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()},
        },
        {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(wav_bytes).decode(), "format": "wav"},
        },
        {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(mp4_bytes).decode()},
        },
        {
            "type": "file",
            "file": {
                "filename": "report.pdf",
                "file_data": base64.b64encode(pdf_bytes).decode(),
            },
        },
        {
            "type": "input_file",
            "filename": "summary.docx",
            "file_data": base64.b64encode(docx_bytes).decode(),
        },
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/octet-stream",
                "data": base64.b64encode(xlsx_bytes).decode(),
            },
        },
        {
            "type": "file",
            "file": {
                "filename": "deck.pptx",
                "data": base64.b64encode(pptx_bytes).decode(),
            },
        },
        {
            "type": "file",
            "file": {"url": str(local_md)},
        },
    ]

    atts = extract_message_attachments(content=content_parts)
    assert len(atts) == 8
    mimes = [a.mime_type for a in atts]
    assert mimes[0] == "image/png"
    assert mimes[1] == "audio/wav"
    assert mimes[2] == "video/mp4"
    assert mimes[3] == "application/pdf"
    assert mimes[4] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert mimes[5] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert mimes[6] == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert mimes[7] == "text/markdown"

    assert "季度营收增长 38%" in (atts[4].extract_text_fallback() or "")
    xlsx_txt = atts[5].extract_text_fallback() or ""
    assert "预算科目A" in xlsx_txt and "42" in xlsx_txt
    assert "产品路线图里程碑" in (atts[6].extract_text_fallback() or "")
    assert "架构评审纪要" in (atts[7].extract_text_fallback() or "")


def test_multimodal_context_builder_delta_dedup_and_reset() -> None:
    import base64

    png_b64 = base64.b64encode(render_spark_icon_png(16)).decode()
    pdf_b64 = base64.b64encode(b"%PDF-1.4\nfirst-doc\n%%EOF").decode()
    wav_b64 = base64.b64encode(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24).decode()

    builder = ConversationContextBuilder(default_mode="smart", auto_reset_new_chat=True)
    session = "mm-session-1"

    # Turn 1: user uploads PNG + PDF
    m1 = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "对比这张架构图和 PDF 规范"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
            {"type": "file", "file": {"filename": "spec.pdf", "file_data": pdf_b64}},
        ],
    )
    p1 = builder.build([m1], model="gemini-4-argon", session_key=session)
    assert p1.reset_chat is True
    assert len(p1.attachments) == 2
    builder.record_completed_turn(session, p1.normalized_history, "已完成架构图与规范对比。")

    # Turn 2 (text-only follow-up): prior PNG + PDF MUST NOT be re-uploaded!
    t2_msgs = [
        m1,
        ChatMessage(role="assistant", content="已完成架构图与规范对比。"),
        ChatMessage(role="user", content="其中第 3 节有哪些潜在风险？"),
    ]
    p2 = builder.build(t2_msgs, model="gemini-4-argon", session_key=session)
    assert p2.strategy_used == "smart_incremental_delta"
    assert p2.reset_chat is False
    assert p2.attachments == []
    assert p2.prompt == "其中第 3 节有哪些潜在风险？"
    builder.record_completed_turn(session, p2.normalized_history, "第 3 节存在超时重试风险。")

    # Turn 3 (follow-up with a NEW audio attachment): sends ONLY the new WAV attachment!
    m3 = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "结合这段会议录音再补充一点"},
            {"type": "input_audio", "input_audio": {"data": wav_b64, "format": "wav"}},
        ],
    )
    t3_msgs = [*t2_msgs, ChatMessage(role="assistant", content="第 3 节存在超时重试风险。"), m3]
    p3 = builder.build(t3_msgs, model="gemini-4-argon", session_key=session)
    assert p3.strategy_used == "smart_incremental_delta"
    assert p3.reset_chat is False
    assert len(p3.attachments) == 1
    assert p3.attachments[0].mime_type == "audio/wav"

    # Explicit -newchat reset on full conversation: re-includes ALL unique attachments across turns!
    p_reset = builder.build(t3_msgs, model="gemini-4-argon-newchat", session_key=session)
    assert p_reset.reset_chat is True
    assert len(p_reset.attachments) == 3

    # Attachment-only user turn (empty text caption) works cleanly
    img_only_msg = ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}}],
    )
    p_img_only = builder.build([img_only_msg], model="gemini-4-argon", session_key="mm-img-only")
    assert len(p_img_only.attachments) == 1
    assert p_img_only.prompt == ""


def test_e2e_browser_attachment_staging_token_guard_and_multi_turn_delta() -> None:
    import base64

    png_bytes = render_spark_icon_png(16)
    pdf_bytes = b"%PDF-1.7\nenterprise-architecture\n%%EOF"
    png_b64 = base64.b64encode(png_bytes).decode()
    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    app = create_app(_make_test_settings())
    captured_cmds: List[Dict[str, Any]] = []
    fetched_blobs: List[bytes] = []

    with TestClient(app) as client:
        ready_ev = threading.Event()

        with client.websocket_connect("/ws/browser?client_id=ext-mm") as ws:
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 77,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home/cid/mm",
                    "ready": True,
                }
            )
            assert ws.receive_json()["type"] == "ack"

            def mm_worker() -> None:
                ready_ev.set()
                for turn_idx in range(2):
                    cmd = ws.receive_json()
                    assert cmd["type"] == "execute_prompt"
                    captured_cmds.append(cmd)
                    req_id = cmd["request_id"]
                    ws.send_json({"type": "request_ack", "request_id": req_id})
                    for att_meta in cmd.get("attachments") or []:
                        # Verify wrong/missing token is rejected with 403
                        bad_r = client.get(f"/api/bridge/attachments/{att_meta['id']}?token=wrong")
                        assert bad_r.status_code == 403
                        ok_r = client.get(att_meta["path"])
                        assert ok_r.status_code == 200
                        fetched_blobs.append(ok_r.content)
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": req_id,
                            "reply": f"多模态回复 #{turn_idx + 1}",
                            "source": "shadow_dom",
                        }
                    )

            th = threading.Thread(target=mm_worker, daemon=True)
            th.start()
            ready_ev.wait(timeout=3.0)

            user_turn_1 = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请审阅图片与 PDF"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
                    {"type": "file", "file": {"filename": "arch.pdf", "file_data": pdf_b64}},
                ],
            }
            r1 = client.post(
                "/v1/chat/completions",
                json={"model": "gemini-4-argon", "messages": [user_turn_1]},
            )
            assert r1.status_code == 200
            reply1 = r1.json()["choices"][0]["message"]["content"]
            assert reply1 == "多模态回复 #1"

            # Turn 2 in same session: follow-up without new attachments -> 0 staged attachments re-sent
            r2 = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-4-argon",
                    "messages": [
                        user_turn_1,
                        {"role": "assistant", "content": reply1},
                        {"role": "user", "content": "总结成三条要点"},
                    ],
                },
            )
            assert r2.status_code == 200
            assert r2.json()["choices"][0]["message"]["content"] == "多模态回复 #2"
            th.join(timeout=3.0)

    assert len(captured_cmds) == 2
    assert len(captured_cmds[0].get("attachments") or []) == 2
    assert fetched_blobs == [png_bytes, pdf_bytes]
    assert (captured_cmds[1].get("attachments") or []) == []
    assert captured_cmds[1]["reset_chat"] is False


def test_gcloud_provider_vertex_and_discovery_multimodal_parts() -> None:
    import base64
    from gcloud_provider import GCloudVertexProvider

    png_bytes = render_spark_icon_png(16)
    docx_bytes = _build_minimal_ooxml_zip("docx", "合同关键条款：保密期五年")
    msg = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "核对图片、GCS 视频与 Word 合同"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}},
            {"type": "video_url", "video_url": {"url": "gs://corp-bucket/demo.mp4", "mime_type": "video/mp4"}},
            {"type": "file", "file": {"filename": "contract.docx", "file_data": base64.b64encode(docx_bytes).decode()}},
        ],
    )
    atts = msg.extract_attachments()
    assert len(atts) == 3

    provider = GCloudVertexProvider(_make_test_settings(gcp_project_id="demo-proj"))

    async def _check() -> None:
        p_img = await provider._attachment_to_vertex_part(atts[0])
        assert p_img["inlineData"]["mimeType"] == "image/png"
        assert base64.b64decode(p_img["inlineData"]["data"]) == png_bytes

        p_gcs = await provider._attachment_to_vertex_part(atts[1])
        assert p_gcs == {"fileData": {"mimeType": "video/mp4", "fileUri": "gs://corp-bucket/demo.mp4"}}

        p_docx = await provider._attachment_to_vertex_part(atts[2])
        assert "合同关键条款：保密期五年" in p_docx["text"]

        d_docx = await provider._attachment_to_discovery_text(atts[2])
        assert "合同关键条款：保密期五年" in d_docx

    asyncio.run(_check())


def test_websocket_browser_accepts_chrome_extension_and_vertex_origins_never_403() -> None:
    ext_origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    vertex_origin = "https://vertexaisearch.cloud.google.com"

    # 1. Default wildcard CORS (allow_origins=["*"], allow_credentials=True)
    app_wildcard = create_app(_make_test_settings(cors_allow_origins=["*"]))
    with TestClient(app_wildcard) as client:
        preflight = client.options(
            "/health",
            headers={
                "Origin": ext_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers.get("access-control-allow-origin") == ext_origin

        health_resp = client.get("/health", headers={"Origin": ext_origin})
        assert health_resp.status_code == 200
        assert health_resp.headers.get("access-control-allow-origin") == ext_origin
        assert health_resp.json()["status"] == "ok"

        for idx, origin in enumerate((ext_origin, vertex_origin)):
            with client.websocket_connect(
                f"/ws/browser?client_id=ext-origin-{idx}",
                headers={"Origin": origin},
            ) as ws:
                welcome = ws.receive_json()
                assert welcome["type"] == "welcome"

    # 2. Restricted HTTP CORS list still allows local /ws/browser extension & userscript handshakes
    app_restricted = create_app(
        _make_test_settings(cors_allow_origins=["http://localhost:3000"])
    )
    with TestClient(app_restricted) as client:
        for idx, origin in enumerate((ext_origin, vertex_origin)):
            with client.websocket_connect(
                f"/ws/browser?client_id=ext-restricted-{idx}",
                headers={"Origin": origin},
            ) as ws:
                welcome = ws.receive_json()
                assert welcome["type"] == "welcome"


def test_models_list_and_retrieve_model_expose_full_1m_budget_fields() -> None:
    """Verify /v1/models and /v1/models/{model_id} expose all BYOK 1M prompt / 65K output fields."""
    app = create_app(_make_test_settings())
    with TestClient(app) as client:
        list_resp = client.get("/v1/models")
        assert list_resp.status_code == 200
        models = list_resp.json()["data"]
        assert len(models) >= 15

        for entry in models:
            assert entry["context_length"] == 1048576
            assert entry["context_window"] == 1048576
            assert entry["max_prompt_tokens"] == 1048576
            assert entry["max_input_tokens"] == 1048576
            assert entry["input_token_limit"] == 1048576
            assert entry["max_output_tokens"] == 65536
            assert entry["max_completion_tokens"] == 65536
            assert entry["output_token_limit"] == 65536
            assert entry["max_tokens"] == 1048576
            assert entry["capabilities"] == {
                "chat": True,
                "tools": True,
                "function_calling": True,
                "streaming": True,
                "json_mode": True,
            }
            assert entry["limits"] == {
                "max_prompt_tokens": 1048576,
                "max_output_tokens": 65536,
                "max_context_window_tokens": 1048576,
            }

        for model_query in (
            "gemini-4-carbon",
            "4argon",
            "3.1pro",
            "gemini-4-carbon@sess1",
            "4argon@sess1-newchat",
            "gemini-web:Gemini 4 Carbon",
        ):
            r = client.get(f"/v1/models/{model_query}")
            assert r.status_code == 200, f"Failed for {model_query}: {r.text}"
            body = r.json()
            assert body["id"] == model_query
            assert body["object"] == "model"
            assert body["max_prompt_tokens"] == 1048576
            assert body["max_output_tokens"] == 65536
            assert body["context_window"] == 1048576
            assert body["limits"]["max_prompt_tokens"] == 1048576
            assert body["limits"]["max_output_tokens"] == 65536

        alias_no_v1 = client.get("/models/4carbon")
        assert alias_no_v1.status_code == 200
        assert alias_no_v1.json()["id"] == "4carbon"
        assert alias_no_v1.json()["max_prompt_tokens"] == 1048576

        missing = client.get("/v1/models/nonexistent-unknown-model")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "model_not_found"


def test_sync_copilot_provider_limits_updates_only_gemini_bridge_models(tmp_path: Path) -> None:
    """Verify sync_client_limits updates NULL/<1M limits for gemini-enterprise without touching other providers."""
    import sqlite3
    from sync_client_limits import sync_copilot_provider_limits

    db_file = tmp_path / "data.db"
    backup_file = tmp_path / "data.db.bak"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE model_providers (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '2026-11-11T00:00:00Z',
            updated_at TEXT NOT NULL DEFAULT '2026-11-11T00:00:00Z',
            type TEXT NOT NULL DEFAULT 'custom',
            settings_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE provider_models (
            id TEXT PRIMARY KEY NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            wire_model TEXT,
            display_name TEXT NOT NULL,
            max_prompt_tokens INTEGER,
            max_output_tokens INTEGER,
            wire_api_override TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-11-11T00:00:00Z',
            updated_at TEXT NOT NULL DEFAULT '2026-11-11T00:00:00Z',
            supported_reasoning_efforts TEXT,
            supports_vision INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO model_providers (id, name, type, settings_json) VALUES (?, ?, ?, ?)",
        (
            "3126a75e-77bb-4baa-b663-6abcc2cdb5a7",
            "gemini-enterprise",
            "custom",
            '{"baseUrl":"http://127.0.0.1:8000/v1","wireApi":"completions"}',
        ),
    )
    conn.execute(
        "INSERT INTO model_providers (id, name, type, settings_json) VALUES (?, ?, ?, ?)",
        (
            "other-prov-1",
            "other-remote",
            "custom",
            '{"baseUrl":"https://api.example.com/v1","wireApi":"completions"}',
        ),
    )
    conn.executemany(
        "INSERT INTO provider_models (id, provider_id, model_id, display_name, max_prompt_tokens, max_output_tokens) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("m1", "3126a75e-77bb-4baa-b663-6abcc2cdb5a7", "gemini-4-carbon", "gemini-4-carbon", None, None),
            ("m2", "3126a75e-77bb-4baa-b663-6abcc2cdb5a7", "gemini-4-argon", "gemini-4-argon", 128000, None),
            ("m3", "other-prov-1", "other-model", "other-model", None, None),
        ],
    )
    conn.commit()
    conn.close()

    dry = sync_copilot_provider_limits(db_path=db_file, dry_run=True)
    assert dry["ok"] is True
    assert len(dry["models_updated"]) == 2

    applied = sync_copilot_provider_limits(db_path=db_file, backup_path=backup_file)
    assert applied["ok"] is True
    assert backup_file.is_file()
    assert len(applied["models_updated"]) == 2

    verify_conn = sqlite3.connect(str(db_file))
    rows = {
        r[0]: (r[1], r[2])
        for r in verify_conn.execute(
            "SELECT id, max_prompt_tokens, max_output_tokens FROM provider_models"
        ).fetchall()
    }
    verify_conn.close()
    assert rows["m1"] == (1048576, 65536)
    assert rows["m2"] == (1048576, 65536)
    assert rows["m3"] == (None, None)

    second = sync_copilot_provider_limits(db_path=db_file)
    assert second["ok"] is True
    assert second["models_updated"] == []


def test_extension_github_release_cors_preflight_guard() -> None:
    import fnmatch

    repo_root = Path(__file__).resolve().parent
    manifest_data = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))
    host_perms = manifest_data.get("host_permissions", [])

    target_cdn_url = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/release-manifest.json"
    )
    assert any(fnmatch.fnmatch(target_cdn_url, pat) for pat in host_perms), (
        f"manifest.json host_permissions {host_perms} must cover release-assets.githubusercontent.com"
    )

    bg_source = (repo_root / "background.js").read_text(encoding="utf-8")
    assert "Cache-Control" not in bg_source, (
        "background.js must not send non-CORS-safelisted Cache-Control request headers"
    )
    assert bg_source.index("api.github.com/repos/") < bg_source.index(
        "releases/latest/download/release-manifest.json"
    )
    assert "if (!backendReturnedRemoteVersion)" in bg_source

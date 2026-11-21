"""End-to-end regression tests for persistent slot affinity, true LRU SQLite archiving, and /r/session/<id> re-entry."""

from __future__ import annotations

from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

from config import BridgeSettings
from context_builder import ConversationContextBuilder
from server import create_app


def _make_settings(tmp_path: Path, **overrides) -> BridgeSettings:
    defaults = dict(
        auto_export_extension=False,
        auto_spawn_tabs=False,
        auto_reset_new_chat=True,
        max_parallel_tabs=2,
        generated_dir=str(tmp_path / "generated"),
        response_store_dir=str(tmp_path / "store"),
        session_archive_path=str(tmp_path / "session_archive.sqlite3"),
    )
    defaults.update(overrides)
    return BridgeSettings(**defaults)


def _recv_next_meaningful(ws) -> dict:
    msg = ws.receive_json()
    chunks = {}
    while msg.get("type") in {"welcome", "ack", "pong", "cancel_request", "execute_prompt_chunk"}:
        if msg.get("type") == "execute_prompt_chunk":
            chunks[msg["chunk_index"]] = msg["data"]
        msg = ws.receive_json()
    if msg.get("type") == "execute_prompt" and msg.get("chunked"):
        msg["prompt"] = "".join(chunks[i] for i in range(msg["total_chunks"]))
    return msg


def test_evicting_stale_seat_preserves_newer_thread_archive(tmp_path: Path):
    builder = ConversationContextBuilder(archive_path=tmp_path / 'sessions.sqlite3')
    base = 'https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/'
    try:
        for slot, reply in [('old', 'first reply'), ('new', 'latest reply')]:
            builder.record_completed_turn(
                slot, [('user', 'prompt')], assistant_reply=reply,
                session_url=base + slot, explicit_session_id='same-thread',
            )
        builder.evict_slot_to_archive('old')
        stored = builder._archive.get_by_explicit_session_id('same-thread')
        assert stored.session_url == base + 'new'
        assert stored.turns[-1] == ('assistant', 'latest reply')
    finally:
        builder.close()


def test_ws_disconnect_reconnect_preserves_slot_and_inherits_prompt_cache_key(tmp_path: Path):
    app = create_app(_make_settings(tmp_path))
    dispatched = []
    sess_url = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-ws-101"

    with TestClient(app) as client:
        # Phase 1: Turn 1 on WS connection #1
        with client.websocket_connect("/ws/browser?client_id=ext-mv3") as ws1:
            ws1.receive_json()  # welcome
            ws1.send_json(
                {
                    "type": "tab_status",
                    "capabilities": ["multi_tab", "reset_tab", "open_session"],
                    "tabs": [
                        {
                            "tab_id": 101,
                            "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                            "ready": True,
                        }
                    ],
                    "ready": True,
                }
            )
            ws1.receive_json()  # ack

            def worker1():
                cmd = _recv_next_meaningful(ws1)
                dispatched.append(cmd)
                ws1.send_json(
                    {
                        "type": "request_complete",
                        "request_id": cmd["request_id"],
                        "tab_id": cmd["tab_id"],
                        "tab_url": sess_url,
                        "tab_title": "Task 101",
                        "reply": "turn1-ok",
                    }
                )

            t1 = threading.Thread(target=worker1, daemon=True)
            t1.start()
            resp1 = client.post(
                "/v1/responses",
                json={
                    "model": "gemini-4-carbon",
                    "prompt_cache_key": "codex-session-mv3",
                    "input": "FIRST_UNIQUE_SEED_101",
                },
            ).json()
            t1.join(timeout=3)
            assert resp1["status"] == "completed"
            assert resp1.get("prompt_cache_key") == "codex-session-mv3"

        # WS1 is now closed (simulating Chrome MV3 service worker 30s idle sleep).
        # Reconnect WS2 with same client_id and tab_id=101.
        with client.websocket_connect("/ws/browser?client_id=ext-mv3") as ws2:
            ws2.receive_json()
            ws2.send_json(
                {
                    "type": "tab_status",
                    "capabilities": ["multi_tab", "reset_tab", "open_session"],
                    "tabs": [{"tab_id": 101, "tab_url": sess_url, "ready": True}],
                    "ready": True,
                }
            )
            ws2.receive_json()

            def worker2():
                for idx in (2, 3):
                    cmd = _recv_next_meaningful(ws2)
                    dispatched.append(cmd)
                    ws2.send_json(
                        {
                            "type": "request_complete",
                            "request_id": cmd["request_id"],
                            "tab_id": cmd["tab_id"],
                            "tab_url": sess_url,
                            "reply": f"turn{idx}-ok",
                        }
                    )

            t2 = threading.Thread(target=worker2, daemon=True)
            t2.start()
            # Turn 2 omits prompt_cache_key, relying on previous_response_id inheritance!
            resp2 = client.post(
                "/v1/responses",
                json={
                    "model": "gemini-4-carbon",
                    "previous_response_id": resp1["id"],
                    "input": "SECOND_DELTA_101",
                },
            ).json()
            assert resp2["status"] == "completed"
            assert resp2.get("prompt_cache_key") == "codex-session-mv3"

            # Turn 3 also omits prompt_cache_key across a 2-hop chain!
            resp3 = client.post(
                "/v1/responses",
                json={
                    "model": "gemini-4-carbon",
                    "previous_response_id": resp2["id"],
                    "input": "THIRD_DELTA_101",
                },
            ).json()
            t2.join(timeout=3)
            assert resp3["status"] == "completed"

    assert len(dispatched) == 3
    assert dispatched[1]["tab_id"] == 101 and dispatched[1]["reset_chat"] is False
    assert "FIRST_UNIQUE_SEED_101" not in dispatched[1]["prompt"]
    assert "SECOND_DELTA_101" in dispatched[1]["prompt"]
    assert dispatched[2]["tab_id"] == 101 and dispatched[2]["reset_chat"] is False
    assert "SECOND_DELTA_101" not in dispatched[2]["prompt"]
    assert "THIRD_DELTA_101" in dispatched[2]["prompt"]


def test_long_idle_session_never_evicted_and_auto_spawns_before_eviction(tmp_path: Path):
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=3, auto_spawn_tabs=True))
    events = []
    url_a = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-idle-A"
    url_b = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-idle-B"

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-spawn") as ws:
        ws.receive_json()
        ws.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "spawn_tab", "open_session"],
                "max_parallel_tabs": 3,
                "tabs": [
                    {
                        "tab_id": 101,
                        "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                        "ready": True,
                    }
                ],
                "ready": True,
            }
        )
        ws.receive_json()

        # 1) Session A runs on tab 101
        def step1():
            cmd = _recv_next_meaningful(ws)
            events.append(cmd)
            ws.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 101,
                    "tab_url": url_a,
                    "reply": "A1-done",
                }
            )

        th = threading.Thread(target=step1, daemon=True)
        th.start()
        resp_a1 = client.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "sess-A", "input": "SEED_TASK_A"},
        ).json()
        th.join(timeout=3)
        assert resp_a1["status"] == "completed"

        # Simulate 4 hours of user inactivity on Session A (well past the old 5-minute TTL)
        app.state.hub._slots["ext-spawn:101"].last_used_at = time.time() - 14400.0

        # 2) Brand-new Session B arrives -> must spawn tab 102 rather than evicting idle tab 101!
        def step2():
            msg = _recv_next_meaningful(ws)
            events.append(msg)
            assert msg["type"] == "spawn_tab"
            ws.send_json(
                {
                    "type": "spawn_tab_result",
                    "spawn_id": msg["spawn_id"],
                    "ok": True,
                    "tab": {
                        "tab_id": 102,
                        "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                        "ready": True,
                    },
                }
            )
            cmd = _recv_next_meaningful(ws)
            events.append(cmd)
            ws.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 102,
                    "tab_url": url_b,
                    "reply": "B1-done",
                }
            )

        th = threading.Thread(target=step2, daemon=True)
        th.start()
        resp_b1 = client.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "sess-B", "input": "SEED_TASK_B"},
        ).json()
        th.join(timeout=3)
        assert resp_b1["status"] == "completed"

        # 3) Session A resumes after 4 hours -> hits original tab 101 incrementally!
        def step3():
            cmd = _recv_next_meaningful(ws)
            events.append(cmd)
            ws.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 101,
                    "tab_url": url_a,
                    "reply": "A2-done",
                }
            )

        th = threading.Thread(target=step3, daemon=True)
        th.start()
        resp_a2 = client.post(
            "/v1/responses",
            json={
                "model": "gemini-4-carbon",
                "previous_response_id": resp_a1["id"],
                "input": "FOLLOWUP_TASK_A",
            },
        ).json()
        th.join(timeout=3)
        assert resp_a2["status"] == "completed"

    prompt_cmds = [e for e in events if e.get("type") == "execute_prompt"]
    assert [c["tab_id"] for c in prompt_cmds] == [101, 102, 101]
    assert prompt_cmds[2]["reset_chat"] is False
    assert "SEED_TASK_A" not in prompt_cmds[2]["prompt"]


def test_full_pool_lru_eviction_and_open_session_reentry_plus_redirect_fallback(tmp_path: Path):
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=2, auto_spawn_tabs=False))
    url_a = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-lru-A"
    url_b = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-lru-B"
    url_c = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-lru-C"

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-lru") as ws:
        ws.receive_json()
        ws.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "open_session"],
                "max_parallel_tabs": 2,
                "tabs": [
                    {"tab_id": 201, "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home", "ready": True},
                    {"tab_id": 202, "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home", "ready": True},
                ],
                "ready": True,
            }
        )
        ws.receive_json()

        def run_simple_turn(target_url: str, reply_text: str) -> dict:
            captured = {}

            def w():
                cmd = _recv_next_meaningful(ws)
                captured["cmd"] = cmd
                ws.send_json(
                    {
                        "type": "request_complete",
                        "request_id": cmd["request_id"],
                        "tab_id": cmd["tab_id"],
                        "tab_url": target_url,
                        "reply": reply_text,
                    }
                )

            t = threading.Thread(target=w, daemon=True)
            t.start()
            captured["thread"] = t
            return captured

        # 1) Populate tab 201 with Session A, tab 202 with Session B
        cap_a = run_simple_turn(url_a, "reply-A1")
        resp_a1 = client.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "lru-A", "input": "ALPHA_INITIAL_HISTORY"},
        ).json()
        cap_a["thread"].join(timeout=3)
        assert cap_a["cmd"]["tab_id"] == 201

        cap_b = run_simple_turn(url_b, "reply-B1")
        resp_b1 = client.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "lru-B", "input": "BETA_INITIAL_HISTORY"},
        ).json()
        cap_b["thread"].join(timeout=3)
        assert cap_b["cmd"]["tab_id"] == 202

        # Make tab 201 strictly older than tab 202
        app.state.hub._slots["ext-lru:201"].last_used_at = 1000.0
        app.state.hub._slots["ext-lru:202"].last_used_at = 2000.0

        # 2) New Session C arrives when pool (2/2) is full -> must evict oldest (tab 201, Session A)!
        cap_c = run_simple_turn(url_c, "reply-C1")
        resp_c1 = client.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "lru-C", "input": "GAMMA_INITIAL_HISTORY"},
        ).json()
        cap_c["thread"].join(timeout=3)
        assert resp_c1["status"] == "completed"
        assert cap_c["cmd"]["tab_id"] == 201
        assert cap_c["cmd"]["reset_chat"] is True

        # Verify Session A is persisted in SQLite archive
        archived_a = app.state.context_builder._archive.get_by_explicit_session_id("lru-A")
        assert archived_a is not None and archived_a.vertex_session_id == "vsid-lru-A"

        # Now tab 202 (last_used_at=2000.0, Session B) is older than tab 201 (Session C, just used).
        # 3) Old Session A returns -> must evict tab 202 (Session B), call open_session(url_a), and send delta with reset_chat=False!
        reentry_events = []

        def w_reenter_a():
            nav = _recv_next_meaningful(ws)
            reentry_events.append(nav)
            assert nav["type"] == "open_session"
            assert nav["tab_id"] == 202
            assert nav["session_url"] == url_a
            ws.send_json(
                {
                    "type": "open_session_result",
                    "open_id": nav["open_id"],
                    "ok": True,
                    "tab_id": 202,
                    "tab_url": url_a,
                    "tab_title": "Resumed A",
                }
            )
            cmd = _recv_next_meaningful(ws)
            reentry_events.append(cmd)
            ws.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 202,
                    "tab_url": url_a,
                    "reply": "reply-A2",
                }
            )

        t_a2 = threading.Thread(target=w_reenter_a, daemon=True)
        t_a2.start()
        resp_a2 = client.post(
            "/v1/responses",
            json={
                "model": "gemini-4-carbon",
                "previous_response_id": resp_a1["id"],
                "input": "ALPHA_STEP_TWO",
            },
        ).json()
        t_a2.join(timeout=3)
        assert resp_a2["status"] == "completed"
        exec_a2 = reentry_events[1]
        assert exec_a2["tab_id"] == 202
        assert exec_a2["reset_chat"] is False
        assert "ALPHA_INITIAL_HISTORY" not in exec_a2["prompt"]
        assert "ALPHA_STEP_TWO" in exec_a2["prompt"]

        # 4) Old Session B returns, but Gemini redirects /r/session/vsid-lru-B back to /home (open_session fails)!
        # Must clean up dead archive entry and gracefully degrade to reset_chat=True with full history.
        fallback_events = []

        def w_fallback_b():
            nav = _recv_next_meaningful(ws)
            fallback_events.append(nav)
            assert nav["type"] == "open_session"
            assert nav["session_url"] == url_b
            ws.send_json(
                {
                    "type": "open_session_result",
                    "open_id": nav["open_id"],
                    "ok": False,
                    "tab_id": nav["tab_id"],
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                    "error": "历史会话已被重定向回空白首页",
                }
            )
            cmd = _recv_next_meaningful(ws)
            fallback_events.append(cmd)
            ws.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": cmd["tab_id"],
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-lru-B-new",
                    "reply": "reply-B2-rebuilt",
                }
            )

        t_b2 = threading.Thread(target=w_fallback_b, daemon=True)
        t_b2.start()
        resp_b2 = client.post(
            "/v1/responses",
            json={
                "model": "gemini-4-carbon",
                "previous_response_id": resp_b1["id"],
                "input": "BETA_STEP_TWO",
            },
        ).json()
        t_b2.join(timeout=3)
        assert resp_b2["status"] == "completed"
        exec_b2 = fallback_events[1]
        assert exec_b2["reset_chat"] is True
        assert "BETA_INITIAL_HISTORY" in exec_b2["prompt"]
        assert "BETA_STEP_TWO" in exec_b2["prompt"]


def test_server_restart_hydrates_live_tabs_from_sqlite_archive(tmp_path: Path):
    url_e = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-restart-301"

    # Process Instance #1
    app1 = create_app(_make_settings(tmp_path))
    with TestClient(app1) as client1, client1.websocket_connect("/ws/browser?client_id=ext-r") as ws1:
        ws1.receive_json()
        ws1.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "open_session"],
                "tabs": [{"tab_id": 301, "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home", "ready": True}],
                "ready": True,
            }
        )
        ws1.receive_json()

        def w1():
            cmd = _recv_next_meaningful(ws1)
            ws1.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 301,
                    "tab_url": url_e,
                    "reply": "E1-done",
                }
            )

        t1 = threading.Thread(target=w1, daemon=True)
        t1.start()
        resp_e1 = client1.post(
            "/v1/responses",
            json={"model": "gemini-4-carbon", "prompt_cache_key": "restart-E", "input": "RESTART_SEED_HISTORY"},
        ).json()
        t1.join(timeout=3)
        assert resp_e1["status"] == "completed"

    # Process Instance #2 (fresh memory, same disk SQLite archive + response_store_dir)
    app2 = create_app(_make_settings(tmp_path))
    post_restart_cmds = []
    with TestClient(app2) as client2, client2.websocket_connect("/ws/browser?client_id=ext-r") as ws2:
        ws2.receive_json()
        # Extension reports tab #301 is still sitting on url_e!
        ws2.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "open_session"],
                "tabs": [{"tab_id": 301, "tab_url": url_e, "ready": True}],
                "ready": True,
            }
        )
        ws2.receive_json()

        def w2():
            cmd = _recv_next_meaningful(ws2)
            post_restart_cmds.append(cmd)
            ws2.send_json(
                {
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": 301,
                    "tab_url": url_e,
                    "reply": "E2-after-restart",
                }
            )

        t2 = threading.Thread(target=w2, daemon=True)
        t2.start()
        resp_e2 = client2.post(
            "/v1/responses",
            json={
                "model": "gemini-4-carbon",
                "previous_response_id": resp_e1["id"],
                "input": "RESTART_DELTA_FOLLOWUP",
            },
        ).json()
        t2.join(timeout=3)
        assert resp_e2["status"] == "completed"

    assert len(post_restart_cmds) == 1
    # Must NOT even need open_session or reset_chat because tab #301 was hydrated directly in place!
    assert post_restart_cmds[0]["type"] == "execute_prompt"
    assert post_restart_cmds[0]["tab_id"] == 301
    assert post_restart_cmds[0]["reset_chat"] is False
    assert "RESTART_SEED_HISTORY" not in post_restart_cmds[0]["prompt"]
    assert "RESTART_DELTA_FOLLOWUP" in post_restart_cmds[0]["prompt"]


def test_post_reboot_probe_prefers_standby_and_never_rehydrates_evicted_project(tmp_path: Path):
    url_p1 = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-proj-newer"
    url_p2 = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-proj-older"
    url_orphan = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-orphan-untracked"
    url_probe = "https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-probe-run"

    now_ts = time.time()
    app_seed = create_app(_make_settings(tmp_path, max_parallel_tabs=3))
    archive = app_seed.state.context_builder._archive
    archive.upsert(
        session_url=url_p1,
        turns=[("user", "构建前端控制台"), ("assistant", "完成")],
        explicit_session_id="codex-proj-1",
        session_title="构建前端控制台",
        active_model="4-carbon",
        updated_at=now_ts - 600.0,
    )
    archive.upsert(
        session_url=url_p2,
        turns=[("user", "优化网络同步模块"), ("assistant", "完成")],
        explicit_session_id="codex-proj-2",
        session_title="优化网络同步模块",
        active_model="4-carbon",
        updated_at=now_ts - 7200.0,
    )
    app_seed.state.context_builder.close()

    app_rb = create_app(_make_settings(tmp_path, max_parallel_tabs=3, auto_spawn_tabs=False))
    probe_cmds = []
    with TestClient(app_rb) as client, client.websocket_connect("/ws/browser?client_id=ext-rb") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "tab_status",
            "capabilities": ["multi_tab", "reset_tab", "open_session"],
            "max_parallel_tabs": 3,
            "tabs": [
                {"tab_id": 401, "tab_url": url_p1, "ready": True},
                {"tab_id": 402, "tab_url": url_p2, "ready": True},
                {"tab_id": 403, "tab_url": url_orphan, "ready": True},
            ],
            "ready": True,
        })
        ws.receive_json()

        for idx in (1, 2):
            def w_probe():
                cmd = _recv_next_meaningful(ws)
                probe_cmds.append(cmd)
                ws.send_json({
                    "type": "request_complete",
                    "request_id": cmd["request_id"],
                    "tab_id": cmd["tab_id"],
                    "tab_url": url_probe,
                    "reply": f"连接正常-{idx}",
                })
            tp = threading.Thread(target=w_probe, daemon=True)
            tp.start()
            res = client.post(
                "/v1/chat/completions",
                headers={"X-Bridge-Probe": "1"},
                json={"model": "gemini-4-carbon", "stream": False, "messages": [{"role": "user", "content": "你好，这是一次连通性体检"}]},
            )
            tp.join(timeout=3)
            assert res.status_code == 200

        assert [c["tab_id"] for c in probe_cmds] == [403, 403]
        mon = {s["tab_id"]: s for s in client.get("/api/status").json()["context_monitor"]["slots"]}
        assert mon[401]["pinned_session_id"] == "codex-proj-1"
        assert mon[402]["pinned_session_id"] == "codex-proj-2"
        assert mon[403]["pinned_session_id"] is None and mon[403]["ephemeral_probe"] is True
        assert app_rb.state.context_builder._archive.get_by_vertex_session_id("vsid-probe-run") is None

    app_full = create_app(_make_settings(tmp_path, max_parallel_tabs=2, auto_spawn_tabs=False))
    mid_snap, evict_cmds = {}, []
    with TestClient(app_full) as client2, client2.websocket_connect("/ws/browser?client_id=ext-full") as ws2:
        ws2.receive_json()
        ws2.send_json({
            "type": "tab_status",
            "capabilities": ["multi_tab", "reset_tab", "open_session"],
            "max_parallel_tabs": 2,
            "tabs": [
                {"tab_id": 401, "tab_url": url_p1, "ready": True},
                {"tab_id": 402, "tab_url": url_p2, "ready": True},
            ],
            "ready": True,
        })
        ws2.receive_json()

        def w_evict():
            cmd = _recv_next_meaningful(ws2)
            evict_cmds.append(cmd)
            mid_snap["data"] = client2.get("/api/status").json()
            ws2.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": cmd["tab_id"],
                "tab_url": url_probe,
                "reply": "连接正常",
            })
        te = threading.Thread(target=w_evict, daemon=True)
        te.start()
        r_ev = client2.post(
            "/v1/chat/completions",
            headers={"X-Bridge-Probe": "1"},
            json={"model": "gemini-4-carbon", "stream": True, "messages": [{"role": "user", "content": "你好，这是一次连通性体检"}]},
        )
        te.join(timeout=3)
        assert r_ev.status_code == 200 and evict_cmds[0]["tab_id"] == 402
        m402 = next(s for s in mid_snap["data"]["context_monitor"]["slots"] if s["tab_id"] == 402)
        assert m402["pinned_session_id"] is None and m402["session_title"] == "一键模型连通体检"
        f402 = next(s for s in client2.get("/api/status").json()["context_monitor"]["slots"] if s["tab_id"] == 402)
        assert f402["pinned_session_id"] is None and f402["ephemeral_probe"] is True
        saved_p2 = app_full.state.context_builder._archive.get_by_explicit_session_id("codex-proj-2")
        assert saved_p2 is not None and saved_p2.vertex_session_id == "vsid-proj-older"


def test_compaction_slot_auto_resets_only_after_resumed_session_starts_working(tmp_path: Path):
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=2, auto_spawn_tabs=False))
    base_url = "https://vertexaisearch.cloud.google.com/u/0/app/cid-main"
    url_501 = f"{base_url}/r/session/vsid-main-501"
    url_502 = f"{base_url}/r/session/vsid-compact-502"
    url_501_resumed = f"{base_url}/r/session/vsid-main-501-next"

    def recv_cmd(ws):
        msg = _recv_next_meaningful(ws)
        while msg.get("type") in {"working_tabs", "sync_busy_tabs"}:
            msg = _recv_next_meaningful(ws)
        return msg

    codex_compact_prompt = (
        "You are performing a context checkpoint compaction. Create a handoff summary "
        "for another LLM that will resume the task."
    )
    compact_summary = (
        "## Goal\nImplement LAN multiplayer co-op sync for Sanctum of Ash.\n"
        "## Next Step\nWire room host heartbeat and snapshot interpolation."
    )
    tools_spec = [{"type": "function", "function": {"name": "exec_command", "parameters": {"type": "object"}}}]

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-cmp") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "tab_status",
            "capabilities": ["multi_tab", "reset_tab", "open_session"],
            "max_parallel_tabs": 2,
            "tabs": [
                {"tab_id": 501, "tab_url": base_url, "ready": True},
                {"tab_id": 502, "tab_url": base_url, "ready": True},
            ],
            "ready": True,
        })
        ws.receive_json()

        # Step 1: Main coding session starts on Tab 501
        def w_turn1():
            cmd = recv_cmd(ws)
            assert cmd["type"] == "execute_prompt" and cmd["tab_id"] == 501
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 501,
                "tab_url": url_501,
                "reply": "已初始化局域网联机模块架构。",
            })

        t1 = threading.Thread(target=w_turn1, daemon=True)
        t1.start()
        r1 = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "codex-lan-coop"},
            json={
                "model": "gemini-4-carbon",
                "stream": False,
                "tools": tools_spec,
                "messages": [{"role": "user", "content": "实现局域网多人合作联机同步"}],
            },
        )
        t1.join(timeout=3)
        assert r1.status_code == 200

        # Step 2: Inline Codex compaction runs on auxiliary Tab 502
        mid_compact_status = {}

        def w_compact():
            cmd = recv_cmd(ws)
            assert cmd["type"] == "execute_prompt" and cmd["tab_id"] == 502
            mid_compact_status["data"] = client.get("/api/status").json()
            ws.send_json({
                "type": "stream_chunk",
                "request_id": cmd["request_id"],
                "tab_id": 502,
                "delta": compact_summary,
            })
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 502,
                "tab_url": url_502,
                "reply": compact_summary,
            })

        tc = threading.Thread(target=w_compact, daemon=True)
        tc.start()
        rc = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "codex-lan-coop"},
            json={
                "model": "gemini-4-carbon",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "实现局域网多人合作联机同步"},
                    {"role": "assistant", "content": "已初始化局域网联机模块架构。"},
                    {"role": "user", "content": codex_compact_prompt},
                ],
            },
        )
        tc.join(timeout=3)
        assert rc.status_code == 200
        mid_502 = next(s for s in mid_compact_status["data"]["context_monitor"]["slots"] if s["tab_id"] == 502)
        assert mid_502["session_title"].startswith("自动整理记忆")
        assert app.state.context_builder._archive.get_by_vertex_session_id("vsid-compact-502") is None
        # Before resumed session starts working, Tab 502 is still tracked as pending handoff
        assert app.state.context_builder.is_ephemeral_compaction_slot(mid_502["slot_key"]) is True

        # Step 3: If the resumed session fails BEFORE submitting to Gemini, Tab 502 must NOT be prematurely reset
        def w_fail_before_submit():
            cmd = recv_cmd(ws)
            assert cmd["type"] == "execute_prompt" and cmd["tab_id"] == 501
            ws.send_json({
                "type": "request_error",
                "request_id": cmd["request_id"],
                "tab_id": 501,
                "submitted": False,
                "error": "Transient network glitch before submit",
            })

        tf = threading.Thread(target=w_fail_before_submit, daemon=True)
        tf.start()
        rf = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "codex-lan-coop"},
            json={
                "model": "gemini-4-carbon",
                "stream": True,
                "tools": tools_spec,
                "messages": [
                    {"role": "user", "content": "实现局域网多人合作联机同步"},
                    {"role": "user", "content": "Another language model started to solve this problem and produced a summary of its thinking process.\n" + compact_summary},
                    {"role": "user", "content": "继续实现房间心跳与快照插值"},
                ],
            },
        )
        _ = rf.text
        tf.join(timeout=3)
        assert app.state.context_builder.is_ephemeral_compaction_slot(mid_502["slot_key"]) is True

        # Step 4: Resumed session successfully starts working on Tab 501 -> Tab 502 auto-resets!
        reset_events = []

        def w_resume_ok():
            cmd = recv_cmd(ws)
            assert cmd["type"] == "execute_prompt" and cmd["tab_id"] == 501
            ws.send_json({
                "type": "request_progress",
                "request_id": cmd["request_id"],
                "tab_id": 501,
                "submitted": True,
                "generating": True,
                "dom_len": 18,
                "net_len": 18,
            })
            ws.send_json({
                "type": "stream_chunk",
                "request_id": cmd["request_id"],
                "tab_id": 501,
                "delta": "开始编写房间心跳逻辑...",
            })
            # Auto-reset for Tab 502 arrives as soon as Tab 501 starts working!
            rst = recv_cmd(ws)
            assert rst["type"] == "reset_tab" and rst["tab_id"] == 502
            reset_events.append(rst)
            ws.send_json({
                "type": "reset_tab_result",
                "reset_id": rst["reset_id"],
                "tab_id": 502,
                "ok": True,
            })
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 501,
                "tab_url": url_501_resumed,
                "reply": "开始编写房间心跳逻辑...已完成。",
            })

        tr = threading.Thread(target=w_resume_ok, daemon=True)
        tr.start()
        rr = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "codex-lan-coop"},
            json={
                "model": "gemini-4-carbon",
                "stream": True,
                "tools": tools_spec,
                "messages": [
                    {"role": "user", "content": "实现局域网多人合作联机同步"},
                    {"role": "user", "content": "Another language model started to solve this problem and produced a summary of its thinking process.\n" + compact_summary},
                    {"role": "user", "content": "继续实现房间心跳与快照插值"},
                ],
            },
        )
        assert "开始编写房间心跳逻辑" in rr.text
        tr.join(timeout=3)
        assert len(reset_events) == 1 and reset_events[0]["tab_id"] == 502

        final_snap = client.get("/api/status").json()
        slots_by_tab = {s["tab_id"]: s for s in final_snap["context_monitor"]["slots"]}
        hub_by_tab = {s["tab_id"]: s for s in final_snap["hub"]["worker_slots"]}
        assert slots_by_tab[501]["pinned_session_id"] == "codex-lan-coop"
        assert slots_by_tab[501]["turn_count"] >= 2
        assert slots_by_tab[502]["turn_count"] == 0
        assert slots_by_tab[502]["session_title"] == ""
        assert slots_by_tab[502]["ephemeral_compaction"] is False
        assert hub_by_tab[502]["pinned_session_id"] is None
        assert "/r/session/" not in hub_by_tab[502]["tab_url"]


def test_responses_compact_token_handoff_and_legacy_archive_cleanup(tmp_path: Path):
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=2, auto_spawn_tabs=False))
    base_url = "https://vertexaisearch.cloud.google.com/u/0/app/cid-resp"
    url_601 = f"{base_url}/r/session/vsid-resp-601"
    url_602 = f"{base_url}/r/session/vsid-compact-602"
    url_legacy_cmp = f"{base_url}/r/session/vsid-legacy-cmp-699"

    def recv_cmd(ws):
        msg = _recv_next_meaningful(ws)
        while msg.get("type") in {"working_tabs", "sync_busy_tabs"}:
            msg = _recv_next_meaningful(ws)
        return msg

    reset_tabs = []
    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-rcmp") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "tab_status",
            "capabilities": ["multi_tab", "reset_tab", "open_session"],
            "max_parallel_tabs": 2,
            "tabs": [
                {"tab_id": 601, "tab_url": base_url, "ready": True},
                {"tab_id": 602, "tab_url": base_url, "ready": True},
            ],
            "ready": True,
        })
        ws.receive_json()

        # 1) Occupy Tab 601 with an active session so /v1/responses/compact picks Tab 602
        def w_seed():
            cmd = recv_cmd(ws)
            assert cmd["tab_id"] == 601
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 601,
                "tab_url": url_601,
                "reply": "初始会话已在601就绪",
            })

        ts = threading.Thread(target=w_seed, daemon=True)
        ts.start()
        assert client.post(
            "/v1/responses",
            headers={"X-Session-Id": "resp-sess-1"},
            json={"input": "启动项目构建", "stream": False},
        ).status_code == 200
        ts.join(timeout=3)

        # 2) Run /v1/responses/compact -> executes on Tab 602
        def w_rcmp():
            cmd = recv_cmd(ws)
            assert cmd["tab_id"] == 602
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 602,
                "tab_url": url_602,
                "reply": "Checkpoint summary: build scripts verified and artifacts staged.",
            })

        trc = threading.Thread(target=w_rcmp, daemon=True)
        trc.start()
        cmp_res = client.post("/v1/responses/compact", json={"input": "构建脚本校验记录"}).json()
        trc.join(timeout=3)
        assert cmp_res["object"] == "response.compaction"

        # 3) Resume via /v1/responses on Tab 601 with compact['output'] -> auto-resets Tab 602!
        def w_rnext():
            cmd = recv_cmd(ws)
            assert cmd["tab_id"] == 601
            ws.send_json({
                "type": "request_complete",
                "request_id": cmd["request_id"],
                "tab_id": 601,
                "tab_url": url_601,
                "reply": "已接管压缩记录并继续部署",
            })
            rst = recv_cmd(ws)
            assert rst["type"] == "reset_tab" and rst["tab_id"] == 602
            reset_tabs.append(rst["tab_id"])
            ws.send_json({
                "type": "reset_tab_result",
                "reset_id": rst["reset_id"],
                "tab_id": 602,
                "ok": True,
            })

        trn = threading.Thread(target=w_rnext, daemon=True)
        trn.start()
        rn = client.post(
            "/v1/responses",
            headers={"X-Session-Id": "resp-sess-1"},
            json={"input": cmp_res["output"] + [{"role": "user", "content": "继续部署"}], "stream": False},
        )
        trn.join(timeout=3)
        assert rn.status_code == 200
        time.sleep(0.05)
        assert reset_tabs == [602]
        s602 = next(s for s in client.get("/api/status").json()["context_monitor"]["slots"] if s["tab_id"] == 602)
        assert s602["turn_count"] == 0 and s602["session_title"] == ""

    # 4) Legacy archived compaction entry in SQLite is purged & auto-reset upon hydration + /api/status
    app2 = create_app(_make_settings(tmp_path, max_parallel_tabs=2, auto_spawn_tabs=False))
    app2.state.context_builder._archive.upsert(
        session_url=url_legacy_cmp,
        turns=[
            ("user", "历史对话内容"),
            ("user", "You are performing a context checkpoint compaction. Create a handoff summary for another LLM."),
            ("assistant", "Legacy summary text"),
        ],
        session_title="遗留压缩快照",
        active_model="gemini-4-carbon",
        metrics={"turn_count": 3, "total_context_tokens": 1200, "updated_at": int(time.time()) - 120},
        updated_at=time.time() - 120.0,
    )
    legacy_resets = []
    with TestClient(app2) as client_leg, client_leg.websocket_connect("/ws/browser?client_id=ext-leg") as ws_leg:
        ws_leg.receive_json()
        ws_leg.send_json({
            "type": "tab_status",
            "capabilities": ["multi_tab", "reset_tab", "open_session"],
            "max_parallel_tabs": 1,
            "tabs": [{"tab_id": 699, "tab_url": url_legacy_cmp, "ready": True}],
            "ready": True,
        })
        ws_leg.receive_json()

        def w_leg_rst():
            rst = recv_cmd(ws_leg)
            assert rst["type"] == "reset_tab" and rst["tab_id"] == 699
            legacy_resets.append(rst["tab_id"])
            ws_leg.send_json({
                "type": "reset_tab_result",
                "reset_id": rst["reset_id"],
                "tab_id": 699,
                "ok": True,
            })

        tl = threading.Thread(target=w_leg_rst, daemon=True)
        tl.start()
        assert client_leg.get("/api/status").status_code == 200
        tl.join(timeout=3)
        time.sleep(0.05)
        assert legacy_resets == [699]
        assert app2.state.context_builder._archive.get_by_vertex_session_id("vsid-legacy-cmp-699") is None
        s699 = client_leg.get("/api/status").json()["context_monitor"]["slots"][0]
        assert s699["turn_count"] == 0 and s699["session_title"] == ""

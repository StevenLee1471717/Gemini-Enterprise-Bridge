"""Check the dashboard script after Python renders the actual HTTP response."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from bridge_hub import BrowserClientState
from codex_activity import lookup_codex_thread_activity
from config import BridgeSettings
from context_builder import ConversationContextBuilder
from schemas import ChatMessage
from server import create_app


def test_served_dashboard_javascript_parses(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the dashboard JavaScript"
    settings = BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path))
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", response.text, re.S | re.I)
    assert scripts, "The dashboard must contain its interactive script"
    for script in scripts:
        result = subprocess.run(
            [node, "--check", "--input-type=commonjs"],
            input=script, capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr


def test_dashboard_ui_structure_and_slot_reset_api(tmp_path: Path) -> None:
    settings = BridgeSettings(
        auto_export_extension=False,
        default_model="gemini-4-carbon",
        generated_dir=str(tmp_path),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        html = client.get("/").text
        for required_id in (
            "stepServiceCard",
            "stepExtCard",
            "stepAuthCard",
            "refreshBtnText",
            "seatDetailPanel",
            "seatDetailEmpty",
            "seatDetailContent",
            "detailResetBtn",
            "detailCompactions",
            "detailCompactionSub",
            "seatsGrid",
            "idleSeatsWrap",
            "idleSeatsToggle",
            "idleSeatsGrid",
            "quickProbeBtn",
            "currencySwitch",
            "ledgerActualMain",
            "ledgerCallsMain",
            "ledgerCacheMain",
            "ledgerCacheSub",
            "ledgerTokensMain",
            "ledgerTokensSub",
            "dailyBarsStage",
            "taskRatioBar",
            "guideModal",
            "secDist",
            "distSavePolicyBtn",
            "distCreateClientBtn",
            "distClientsTable",
            "detailTenantChip",
            "secDiag",
        ):
            assert required_id in html
        for removed_marker in (
            "ledgerSavedMain",
            "reuseRateChip",
            "多轮续聊累计省下",
            "全量重发原价",
            "续聊省下",
            "主活跃对话（",
            "主活跃对话记忆池",
        ):
            assert removed_marker not in html
        assert "缓存命中率" in html
        assert "累计 Token" in html
        assert "缓存 0 / 总输入 0" in html
        assert "输入 0 / 输出 0 / 缓存 0" in html
        assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in html
        assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in html
        assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in html
        assert "[hidden] { display: none !important; }" in html
        assert "@keyframes refreshSpin" in html

        status = client.get("/api/status").json()
        assert status["default_model"] == "gemini-4-carbon"
        assert status["api_key_required"] is False

        # Missing slot -> 404
        missing = client.post("/api/slots/reset", json={"slot_key": "ext-none:999"})
        assert missing.status_code == 404

        # Register a mock slot, populate context turns, and reset when idle
        hub = app.state.hub
        ctx_builder = app.state.context_builder
        slot = hub._upsert_single_slot(
            client_id="ext-test",
            tab_id=101,
            tab_url="https://vertexaisearch.cloud.google.com/u/1/home",
            tab_title="Gemini Enterprise",
            tab_ready=True,
            active_model="4-carbon",
        )
        ctx_builder._session_metrics[slot.slot_key] = {
            "turn_count": 6,
            "user_turns": 3,
            "total_context_tokens": 42000,
        }

        # Busy slot must reject reset with 409
        slot.in_flight_request_id = "req-busy"
        busy_res = client.post("/api/slots/reset", json={"slot_key": slot.slot_key})
        assert busy_res.status_code == 409

        # Idle slot resets cleanly and clears context metrics
        slot.in_flight_request_id = None
        ok_res = client.post("/api/slots/reset", json={"slot_key": slot.slot_key})
        assert ok_res.status_code == 200
        assert ok_res.json()["ok"] is True
        assert ctx_builder.get_slot_context_metrics(slot.slot_key) is None


def test_slot_session_title_three_tier_resolution(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    tid_index = "01a1d51f-a5a7-72b1-b8a9-ae451b402ed4"
    tid_sqlite = "01a1d5ae-6d14-7142-9c01-167fc5126869"

    (codex_dir / "session_index.jsonl").write_text(
        json.dumps({"id": tid_index, "thread_name": "分析前端重构需求"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(str(codex_dir / "state_5.sqlite"))
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)")
    con.execute(
        "INSERT INTO threads (id, name, title) VALUES (?, ?, ?)",
        (tid_sqlite, "继续写实 Roguelike 项目", "raw prompt"),
    )
    con.commit()
    con.close()

    cb = ConversationContextBuilder(codex_home=codex_dir)

    # Tier 2: Immediate live prompt title extraction before Turn 1 finishes
    cb.build(
        messages=[
            ChatMessage(role="user", content="# AGENTS.md instructions for /tmp\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
            ChatMessage(role="user", content="<environment_context><cwd>/tmp</cwd></environment_context>"),
            ChatMessage(
                role="user",
                content="## Referenced chats:\n[]\n## My request:\n优化 [@记账看板](thread://123) 的柱状图显示",
            ),
        ],
        session_key="ext:202",
    )

    snap = cb.get_context_snapshot(
        [
            {
                "slot_key": "ext:201",
                "tab_id": 201,
                "busy": True,
                "pinned_session_id": tid_index,
                "tab_title": "[System Instructions] You are Codex. You are running...",
                "completed_requests": 5,
            },
            {
                "slot_key": "ext:202",
                "tab_id": 202,
                "busy": True,
                "pinned_session_id": None,
                "tab_title": "[System Instructions] You are Codex...",
                "completed_requests": 0,
            },
            {
                "slot_key": "ext:203",
                "tab_id": 203,
                "busy": False,
                "pinned_session_id": tid_sqlite,
                "tab_title": "Tool stream disconnected - Gemini Enterprise",
                "completed_requests": 12,
            },
            {
                "slot_key": "ext:204",
                "tab_id": 204,
                "busy": False,
                "pinned_session_id": None,
                "tab_title": "微积分极限推导整理 - Gemini Enterprise",
                "completed_requests": 2,
            },
            {
                "slot_key": "ext:205",
                "tab_id": 205,
                "busy": False,
                "pinned_session_id": None,
                "tab_title": "文件完整性检查要点 - Gemini Enterprise",
                "completed_requests": 0,
            },
        ]
    )
    by_key = {s["slot_key"]: s for s in snap["slots"]}
    assert by_key["ext:201"]["session_title"] == "分析前端重构需求"
    assert by_key["ext:201"]["title_source"] == "codex"
    assert by_key["ext:202"]["session_title"] == "优化 @记账看板 的柱状图显示"
    assert by_key["ext:202"]["title_source"] == "prompt"
    assert by_key["ext:203"]["session_title"] == "继续写实 Roguelike 项目"
    assert by_key["ext:203"]["title_source"] == "codex"
    assert by_key["ext:204"]["session_title"] == "微积分极限推导整理"
    assert by_key["ext:204"]["title_source"] == "web"
    # Untouched standby tab ignores stale leftover browser title
    assert by_key["ext:205"]["session_title"] == ""


def test_codex_rollout_lifecycle_prevents_idle_flicker_and_guards_reset(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    rollout_dir = codex_dir / "sessions" / "2026" / "11" / "14"
    rollout_dir.mkdir(parents=True)
    tid = "01a1d999-1111-7222-8333-444455556666"
    rollout_file = rollout_dir / f"rollout-2026-11-14T14-00-00-{tid}.jsonl"

    def append_rows(*rows: dict) -> None:
        with rollout_file.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    con = sqlite3.connect(str(codex_dir / "state_5.sqlite"))
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, rollout_path TEXT)")
    con.execute(
        "INSERT INTO threads (id, name, title, rollout_path) VALUES (?, ?, ?, ?)",
        (tid, "重构席位状态监控", "prompt", str(rollout_file)),
    )
    con.commit()
    con.close()

    settings = BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path / "gen"))
    app = create_app(settings)
    app.state.context_builder._codex_home = codex_dir
    hub = app.state.hub
    hub._clients["ext-codex"] = BrowserClientState(client_id="ext-codex", transport="http_poll")
    slot = hub._upsert_single_slot(
        client_id="ext-codex",
        tab_id=301,
        tab_url="https://vertexaisearch.cloud.google.com/u/1/home/cid/abc/r/session/999",
        tab_title="Gemini Enterprise",
        tab_ready=True,
        active_model="4-carbon",
    )
    slot.pinned_session_id = tid
    hub._session_affinity[tid] = slot.slot_key

    with TestClient(app) as client:
        # 1) Turn starts: before HTTP hits Bridge, seat is already marked waiting for model
        append_rows(
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "检查状态"}},
        )
        st1 = client.get("/api/status").json()["context_monitor"]["slots"][0]
        assert st1["session_active"] is True
        assert st1["activity_phase"] == "waiting_model"
        assert st1["activity_label"] == "工作中 · 等待回复"

        # 2) Parallel tool calls emitted; HTTP stream closes (slot.busy == False), tools run locally
        append_rows(
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
        )
        st2 = client.get("/api/status").json()["context_monitor"]["slots"][0]
        assert st2["busy"] is False
        assert st2["session_active"] is True
        assert st2["activity_phase"] == "running_tool"
        assert st2["activity_label"] == "工作中 · 执行命令"
        assert st2["tool_name"] == "apply_patch"

        # Reset must be blocked with 409 while Codex turn is active even when HTTP is idle!
        blocked_rst = client.post("/api/slots/reset", json={"slot_key": slot.slot_key})
        assert blocked_rst.status_code == 409

        # 3) Second tool finishes -> preparing/waiting for next model turn
        append_rows(
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": "done"}},
        )
        st3 = client.get("/api/status").json()["context_monitor"]["slots"][0]
        assert st3["session_active"] is True
        assert st3["activity_phase"] == "waiting_model"
        assert st3["activity_label"] == "工作中 · 等待回复"

        # 4) While Bridge HTTP request is in flight -> generating
        slot.in_flight_request_id = "req-live"
        st4 = client.get("/api/status").json()["context_monitor"]["slots"][0]
        assert st4["session_active"] is True
        assert st4["activity_phase"] == "generating"
        assert st4["activity_label"] == "工作中 · 模型回复"
        slot.in_flight_request_id = None

        # 5) Staleness guard: if Codex process died > 15m ago without task_complete, auto-recover to idle
        stale_check = lookup_codex_thread_activity(
            tid,
            codex_home=codex_dir,
            now_ts=rollout_file.stat().st_mtime + 1200.0,
        )
        assert stale_check["stale"] is True
        assert stale_check["active"] is False

        # 6) Turn completes -> immediately returns to idle and allows slot reset
        append_rows({"type": "event_msg", "payload": {"type": "task_complete"}})
        st6 = client.get("/api/status").json()["context_monitor"]["slots"][0]
        assert st6["session_active"] is False
        assert st6["activity_phase"] == "idle"
        assert st6["activity_label"] == "空闲待命"

        ok_rst = client.post("/api/slots/reset", json={"slot_key": slot.slot_key})
        assert ok_rst.status_code == 200


def test_status_reflects_extension_max_parallel_tabs(tmp_path: Path) -> None:
    settings = BridgeSettings(
        auto_export_extension=False,
        max_parallel_tabs=10,
        generated_dir=str(tmp_path),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/browser?client_id=ext-cap") as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            ws.send_json(
                {
                    "type": "tab_status",
                    "max_parallel_tabs": 6,
                    "capabilities": ["multi_tab", "spawn_tab"],
                    "tabs": [
                        {
                            "tab_id": 101,
                            "tab_url": "https://vertexaisearch.cloud.google.com/home/cid/demo",
                            "tab_title": "Demo",
                            "ready": True,
                        }
                    ],
                }
            )
            # Give the async WS loop a brief moment via status poll
            import time

            hub = {}
            for _ in range(20):
                hub = client.get("/api/status").json()["hub"]
                if hub.get("client_max_parallel_tabs") == 6:
                    break
                time.sleep(0.02)
            assert hub["max_parallel_tabs"] == 6
            assert hub["client_max_parallel_tabs"] == 6
            assert hub["server_max_parallel_tabs"] == 10
            assert hub["active_client"]["max_parallel_tabs"] == 6


def test_slot_compaction_count_tracks_rollout_and_inline_summaries(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    rollout_dir = codex_dir / "sessions" / "2026" / "11" / "14"
    rollout_dir.mkdir(parents=True)
    tid = "01a1d888-2222-7333-8444-555566667777"
    rollout_file = rollout_dir / f"rollout-2026-11-14T18-00-00-{tid}.jsonl"

    def append_rows(*rows: dict) -> None:
        with rollout_file.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    con = sqlite3.connect(str(codex_dir / "state_5.sqlite"))
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, rollout_path TEXT)")
    con.execute(
        "INSERT INTO threads (id, name, title, rollout_path) VALUES (?, ?, ?, ?)",
        (tid, "压缩计数测试会话", "prompt", str(rollout_file)),
    )
    con.commit()
    con.close()

    cb = ConversationContextBuilder(
        codex_home=codex_dir,
        archive_path=tmp_path / "archive.sqlite3",
    )
    try:
        append_rows(
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "compacted", "payload": {"window_number": 1, "message": "summary 1"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        )
        snap1 = cb.get_context_snapshot(
            [
                {
                    "slot_key": "ext:501",
                    "tab_id": 501,
                    "busy": False,
                    "pinned_session_id": tid,
                }
            ]
        )
        assert snap1["slots"][0]["compaction_count"] == 1

        append_rows(
            {"type": "compacted", "payload": {"window_number": 2, "message": "summary 2"}},
        )
        snap2 = cb.get_context_snapshot(
            [
                {
                    "slot_key": "ext:501",
                    "tab_id": 501,
                    "busy": False,
                    "pinned_session_id": tid,
                }
            ]
        )
        assert snap2["slots"][0]["compaction_count"] == 2

        # Non-Codex inline compaction summary tracking across multiple compactions
        cb.record_completed_turn(
            session_key="ext:502",
            sent_history=[("user", "[Previous session summary]\nCheckpoint A"), ("user", "继续任务")],
            assistant_reply="好的",
            last_strategy="smart_packed_context",
            session_url="https://vertexaisearch.cloud.google.com/u/1/home/cid/x/r/session/1001",
            explicit_session_id="custom-sess-1",
        )
        assert cb.get_slot_context_metrics("ext:502")["compaction_count"] == 1

        cb.record_completed_turn(
            session_key="ext:502",
            sent_history=[("user", "[Previous session summary]\nCheckpoint B"), ("user", "第二步")],
            assistant_reply="完成",
            last_strategy="smart_packed_context",
            session_url="https://vertexaisearch.cloud.google.com/u/1/home/cid/x/r/session/1002",
            explicit_session_id="custom-sess-1",
        )
        assert cb.get_slot_context_metrics("ext:502")["compaction_count"] == 2
    finally:
        cb.close()

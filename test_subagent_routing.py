"""Regression tests for Codex multi-subagent isolation, parallel slot overflow, and auto-spawning tabs."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from config import BridgeSettings
from context_builder import lookup_codex_thread_title
from server import create_app


def _make_settings(tmp_path: Path, **overrides) -> BridgeSettings:
    defaults = dict(
        auto_export_extension=False,
        auto_spawn_tabs=True,
        auto_reset_new_chat=True,
        max_parallel_tabs=4,
        queue_timeout_seconds=2.0,
        generated_dir=str(tmp_path / "generated"),
        response_store_dir=str(tmp_path / "store"),
        session_archive_path=str(tmp_path / "session_archive.sqlite3"),
    )
    defaults.update(overrides)
    return BridgeSettings(**defaults)


def _recv_meaningful(ws) -> dict:
    chunks: dict[int, str] = {}
    while True:
        msg = ws.receive_json()
        mtype = msg.get("type")
        if mtype in {"welcome", "ack", "pong", "working_tabs", "sync_busy_tabs", "cancel_request"}:
            continue
        if mtype == "execute_prompt_chunk":
            chunks[msg["chunk_index"]] = msg["data"]
            continue
        if mtype == "execute_prompt" and msg.get("chunked"):
            msg["prompt"] = "".join(chunks[i] for i in range(msg["total_chunks"]))
            chunks.clear()
        return msg


def test_codex_subagents_with_shared_prompt_cache_key_isolated_and_auto_spawn_tabs(tmp_path: Path):
    """
    Reproduces the real Codex Desktop multi-agent pattern:
    Parent (`01a1e42a`) and spawned subagents (`01a1e42c`, `01a1e42d`) share the root
    `prompt_cache_key` and `session-id`, while carrying distinct `thread-id` /
    `client_metadata.thread_id` / `x-codex-window-id`.
    Subagents must run concurrently on idle + auto-spawned tabs without queueing on or
    resetting the parent's tab, and all 3 agents must preserve incremental multi-turn context.
    """
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=4, auto_spawn_tabs=True))
    base_url = "https://vertexaisearch.cloud.google.com/u/0/app/cid-sub"
    root_sid = "01a1e42a-025d-7b92-bfeb-b26ef43e1bdd"
    sub1_tid = "01a1e42c-8010-7ba0-890b-579e95974160"
    sub2_tid = "01a1e42d-07d3-7df3-abf9-12fc7a64a35a"

    tools_payload = [
        {
            "type": "function",
            "name": "exec_command",
            "description": "Run shell command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        }
    ]

    dispatched: list[dict] = []
    spawned_tabs: list[int] = []
    both_subagents_in_flight = threading.Event()
    in_flight_subagent_tabs: set[int] = set()
    lock = threading.Lock()
    stop_worker = False

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-codex-sub") as ws:
        ws.receive_json()  # welcome
        ws.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "spawn_tab", "open_session"],
                "max_parallel_tabs": 4,
                "tabs": [
                    {"tab_id": 101, "tab_url": base_url, "ready": True},
                    {"tab_id": 102, "tab_url": base_url, "ready": True},
                ],
                "ready": True,
            }
        )
        ws.receive_json()  # ack

        next_spawn_id = 103

        def browser_driver():
            nonlocal next_spawn_id
            while not stop_worker:
                try:
                    msg = _recv_meaningful(ws)
                except Exception:
                    break
                mtype = msg.get("type")
                if mtype == "spawn_tab":
                    tid = next_spawn_id
                    next_spawn_id += 1
                    spawned_tabs.append(tid)
                    ws.send_json(
                        {
                            "type": "spawn_tab_result",
                            "spawn_id": msg["spawn_id"],
                            "ok": True,
                            "tab": {"tab_id": tid, "tab_url": base_url, "tab_title": f"Worker {tid}"},
                        }
                    )
                    continue
                if mtype == "reset_tab":
                    ws.send_json(
                        {"type": "reset_tab_result", "reset_id": msg["reset_id"], "tab_id": msg["tab_id"], "ok": True}
                    )
                    continue
                if mtype != "execute_prompt":
                    continue

                dispatched.append(msg)
                prompt = msg["prompt"]
                tid = msg["tab_id"]

                if "PARENT_SEED_ALPHA" in prompt and "PARENT_DELTA_OMEGA" not in prompt:
                    reply = '```codex_tool_call\n{"name": "exec_command", "arguments": {"cmd": "pwd"}}\n```'
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": msg["request_id"],
                            "tab_id": tid,
                            "tab_url": f"{base_url}/r/session/vsid-tab-{tid}",
                            "reply": reply,
                        }
                    )
                elif "SUB1_SEED_SARTRE" in prompt and "SUB1_DELTA_2" not in prompt or (
                    "SUB2_SEED_BOOLE" in prompt and "SUB2_DELTA_2" not in prompt
                ):
                    with lock:
                        in_flight_subagent_tabs.add(tid)
                        if len(in_flight_subagent_tabs) >= 2:
                            both_subagents_in_flight.set()
                    # Hold until BOTH subagents are simultaneously active on separate tabs!
                    def finish_subagent(req_msg=msg, tab_id=tid):
                        both_subagents_in_flight.wait(timeout=1.5)
                        tag = "SARTRE_OK_1" if "SUB1_SEED_SARTRE" in req_msg["prompt"] else "BOOLE_OK_1"
                        ws.send_json(
                            {
                                "type": "request_complete",
                                "request_id": req_msg["request_id"],
                                "tab_id": tab_id,
                                "tab_url": f"{base_url}/r/session/vsid-tab-{tab_id}",
                                "reply": tag,
                            }
                        )

                    threading.Thread(target=finish_subagent, daemon=True).start()
                else:
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": msg["request_id"],
                            "tab_id": tid,
                            "tab_url": f"{base_url}/r/session/vsid-tab-{tid}",
                            "reply": f"CONTINUATION_OK_ON_{tid}",
                        }
                    )

        driver_thread = threading.Thread(target=browser_driver, daemon=True)
        driver_thread.start()

        # 1) Parent Turn 1 on Tab 101 (emits tool call -> sets pending_tool_call_until)
        p1 = client.post(
            "/v1/responses",
            headers={
                "session-id": root_sid,
                "thread-id": root_sid,
                "x-codex-window-id": f"{root_sid}:0",
            },
            json={
                "model": "gemini-4-carbon",
                "prompt_cache_key": root_sid,
                "client_metadata": {"session_id": root_sid, "thread_id": root_sid},
                "tools": tools_payload,
                "input": [{"role": "user", "content": "PARENT_SEED_ALPHA"}],
            },
        ).json()
        assert p1["status"] == "completed"
        assert p1["prompt_cache_key"] == root_sid
        call_item = next(it for it in p1["output"] if it["type"] == "function_call")

        # 2) Subagent 1 & Subagent 2 fire concurrently while sharing `prompt_cache_key == root_sid`!
        sub_results: dict[str, dict] = {}

        def run_sub(label: str, sub_tid: str, seed_text: str):
            resp = client.post(
                "/v1/responses",
                headers={
                    "session-id": root_sid,
                    "thread-id": sub_tid,
                    "x-codex-window-id": f"{sub_tid}:0",
                    "x-codex-turn-metadata": json.dumps(
                        {"session_id": root_sid, "thread_id": sub_tid, "thread_source": "subagent"}
                    ),
                },
                json={
                    "model": "gemini-4-carbon",
                    "prompt_cache_key": root_sid,
                    "client_metadata": {
                        "session_id": root_sid,
                        "thread_id": sub_tid,
                        "x-codex-window-id": f"{sub_tid}:0",
                    },
                    "tools": tools_payload,
                    "input": [{"role": "user", "content": seed_text}],
                },
            )
            sub_results[label] = resp.json()

        t_s1 = threading.Thread(target=run_sub, args=("s1", sub1_tid, "SUB1_SEED_SARTRE"))
        t_s2 = threading.Thread(target=run_sub, args=("s2", sub2_tid, "SUB2_SEED_BOOLE"))
        t_s1.start()
        t_s2.start()
        t_s1.join(timeout=4)
        t_s2.join(timeout=4)

        assert both_subagents_in_flight.is_set(), "Both subagents should execute in parallel without queueing!"
        assert spawned_tabs == [103], "Bridge should auto-spawn Tab 103 once Tab 101 & 102 are occupied!"
        assert sub_results["s1"]["status"] == "completed"
        assert sub_results["s2"]["status"] == "completed"
        assert sub_results["s1"]["prompt_cache_key"] == root_sid
        assert sub_results["s2"]["prompt_cache_key"] == root_sid

        # GET /v1/responses/{id} must not leak internal `_bridge_thread_id` field
        fetched_s1 = client.get(f"/v1/responses/{sub_results['s1']['id']}").json()
        assert "_bridge_thread_id" not in fetched_s1

        # 3) Turn 2 for Subagent 1, Subagent 2, and Parent must each hit incremental delta on their own tab!
        s1_turn2 = client.post(
            "/v1/responses",
            headers={"session-id": root_sid, "thread-id": sub1_tid},
            json={
                "model": "gemini-4-carbon",
                "prompt_cache_key": root_sid,
                "previous_response_id": sub_results["s1"]["id"],
                "tools": tools_payload,
                "input": [{"role": "user", "content": "SUB1_DELTA_2"}],
            },
        ).json()
        assert s1_turn2["status"] == "completed"

        # A shared session header must not override the thread inherited from previous_response_id.
        s2_turn2 = client.post(
            "/v1/responses",
            headers={"x-session-id": root_sid, "x-bridge-session": root_sid},
            json={
                "model": "gemini-4-carbon",
                "prompt_cache_key": root_sid,
                "previous_response_id": sub_results["s2"]["id"],
                "tools": tools_payload,
                "input": [{"role": "user", "content": "SUB2_DELTA_2"}],
            },
        ).json()
        assert s2_turn2["status"] == "completed"

        p_turn2 = client.post(
            "/v1/responses",
            headers={"session-id": root_sid, "thread-id": root_sid},
            json={
                "model": "gemini-4-carbon",
                "prompt_cache_key": root_sid,
                "previous_response_id": p1["id"],
                "tools": tools_payload,
                "input": [
                    {"type": "function_call_output", "call_id": call_item["call_id"], "output": "/workspace"},
                    {"role": "user", "content": "PARENT_DELTA_OMEGA"},
                ],
            },
        ).json()
        assert p_turn2["status"] == "completed"

        stop_worker = True

    assert len(dispatched) == 6
    parent_t1 = dispatched[0]
    assert parent_t1["tab_id"] == 101
    sub_tabs = {dispatched[1]["tab_id"], dispatched[2]["tab_id"]}
    assert sub_tabs == {102, 103}

    # All three Turn-2 continuations must be incremental (`reset_chat == False`) on their own tabs!
    for turn2_cmd, seed_marker, delta_marker in (
        (dispatched[3], "SUB1_SEED_SARTRE", "SUB1_DELTA_2"),
        (dispatched[4], "SUB2_SEED_BOOLE", "SUB2_DELTA_2"),
        (dispatched[5], "PARENT_SEED_ALPHA", "PARENT_DELTA_OMEGA"),
    ):
        assert turn2_cmd["reset_chat"] is False
        assert seed_marker not in turn2_cmd["prompt"]
        assert delta_marker in turn2_cmd["prompt"]
    assert dispatched[5]["tab_id"] == 101


def test_shared_session_id_without_thread_id_overflows_and_spawns_concurrently(tmp_path: Path):
    """
    Even when a client sends NO `thread-id` at all and reuses a single `X-Session-Id`
    across a mid-tool parent session and two concurrent child branches, Bridge must
    auto-spawn worker tabs rather than queueing or clobbering the parent tab.
    """
    app = create_app(_make_settings(tmp_path, max_parallel_tabs=4, auto_spawn_tabs=True))
    base_url = "https://vertexaisearch.cloud.google.com/u/0/app/cid-shared"
    tools_spec = [
        {
            "type": "function",
            "function": {"name": "run_cmd", "parameters": {"type": "object", "properties": {"c": {"type": "string"}}}},
        }
    ]
    both_children_active = threading.Event()
    active_child_tabs: set[int] = set()
    dispatched: list[dict] = []
    lock = threading.Lock()
    stop_worker = False

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-shared") as ws:
        ws.receive_json()
        ws.send_json(
            {
                "type": "tab_status",
                "capabilities": ["multi_tab", "reset_tab", "spawn_tab", "open_session"],
                "max_parallel_tabs": 4,
                "tabs": [{"tab_id": 201, "tab_url": base_url, "ready": True}],
                "ready": True,
            }
        )
        ws.receive_json()
        next_tid = 202

        def browser_driver():
            nonlocal next_tid
            while not stop_worker:
                try:
                    msg = _recv_meaningful(ws)
                except Exception:
                    break
                if msg.get("type") == "spawn_tab":
                    tid = next_tid
                    next_tid += 1
                    ws.send_json(
                        {
                            "type": "spawn_tab_result",
                            "spawn_id": msg["spawn_id"],
                            "ok": True,
                            "tab": {"tab_id": tid, "tab_url": base_url, "tab_title": f"Spawn {tid}"},
                        }
                    )
                    continue
                if msg.get("type") != "execute_prompt":
                    continue
                dispatched.append(msg)
                tid = msg["tab_id"]
                prompt = msg["prompt"]
                if "ROOT_TOOL_TURN_1" in prompt and "ROOT_TOOL_TURN_2" not in prompt:
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": msg["request_id"],
                            "tab_id": tid,
                            "tab_url": f"{base_url}/r/session/vsid-{tid}",
                            "reply": '```codex_tool_call\n{"name": "run_cmd", "arguments": {"c": "ls"}}\n```',
                        }
                    )
                elif "CHILD_BRANCH_" in prompt and "FOLLOWUP" not in prompt:
                    with lock:
                        active_child_tabs.add(tid)
                        if len(active_child_tabs) >= 2:
                            both_children_active.set()

                    def finish_child(req_msg=msg, tab_id=tid):
                        both_children_active.wait(timeout=1.5)
                        ans = "CHILD_A_REPLY" if "CHILD_BRANCH_A" in req_msg["prompt"] else "CHILD_B_REPLY"
                        ws.send_json(
                            {
                                "type": "request_complete",
                                "request_id": req_msg["request_id"],
                                "tab_id": tab_id,
                                "tab_url": f"{base_url}/r/session/vsid-{tab_id}",
                                "reply": ans,
                            }
                        )

                    threading.Thread(target=finish_child, daemon=True).start()
                else:
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": msg["request_id"],
                            "tab_id": tid,
                            "tab_url": f"{base_url}/r/session/vsid-{tid}",
                            "reply": f"DONE_{tid}",
                        }
                    )

        threading.Thread(target=browser_driver, daemon=True).start()

        # Parent Turn 1 on Tab 201
        r_root1 = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "shared-only-sid"},
            json={
                "model": "gemini-4-carbon",
                "tools": tools_spec,
                "messages": [{"role": "user", "content": "ROOT_TOOL_TURN_1"}],
            },
        ).json()
        tc = r_root1["choices"][0]["message"]["tool_calls"][0]

        # Two concurrent child requests sharing the exact same X-Session-Id while Tab 201 is awaiting tool output!
        child_out: dict[str, dict] = {}

        def call_child(key: str, text: str):
            child_out[key] = client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "shared-only-sid"},
                json={
                    "model": "gemini-4-carbon",
                    "tools": tools_spec,
                    "messages": [{"role": "user", "content": text}],
                },
            ).json()

        ta = threading.Thread(target=call_child, args=("a", "CHILD_BRANCH_A"))
        tb = threading.Thread(target=call_child, args=("b", "CHILD_BRANCH_B"))
        ta.start()
        tb.start()
        ta.join(timeout=4)
        tb.join(timeout=4)

        assert both_children_active.is_set()
        assert active_child_tabs == {202, 203}

        # Follow-up Turn 2 for Child B and Root must each route back to their own tab incrementally!
        r_child_b2 = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "shared-only-sid"},
            json={
                "model": "gemini-4-carbon",
                "tools": tools_spec,
                "messages": [
                    {"role": "user", "content": "CHILD_BRANCH_B"},
                    {"role": "assistant", "content": "CHILD_B_REPLY"},
                    {"role": "user", "content": "CHILD_BRANCH_B_FOLLOWUP"},
                ],
            },
        )
        assert r_child_b2.status_code == 200

        r_root2 = client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "shared-only-sid"},
            json={
                "model": "gemini-4-carbon",
                "tools": tools_spec,
                "messages": [
                    {"role": "user", "content": "ROOT_TOOL_TURN_1"},
                    {"role": "assistant", "content": None, "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": tc["id"], "content": "file1.py"},
                    {"role": "user", "content": "ROOT_TOOL_TURN_2"},
                ],
            },
        )
        assert r_root2.status_code == 200
        stop_worker = True

    assert dispatched[-2]["reset_chat"] is False
    assert "CHILD_BRANCH_B_FOLLOWUP" in dispatched[-2]["prompt"]
    assert dispatched[-1]["tab_id"] == 201
    assert dispatched[-1]["reset_chat"] is False
    assert "ROOT_TOOL_TURN_2" in dispatched[-1]["prompt"]


def test_codex_subagent_sqlite_title_formatting(tmp_path: Path):
    codex_dir = tmp_path / "codex_home"
    codex_dir.mkdir()
    db = codex_dir / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, agent_nickname TEXT, thread_source TEXT)"
    )
    con.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
        ("01a1e42c-8010-7ba0-890b-579e95974160", None, "Audit Windows paths", "Sartre", "subagent"),
    )
    con.commit()
    con.close()

    title = lookup_codex_thread_title("01a1e42c-8010-7ba0-890b-579e95974160", codex_home=codex_dir)
    assert title == "子代理 Sartre · Audit Windows paths"

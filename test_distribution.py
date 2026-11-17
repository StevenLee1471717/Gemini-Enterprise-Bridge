"""End-to-end tests for public API distribution, zero cross-talk isolation, and host-reserved scheduling."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from config import BridgeSettings
from distribution import DistributionManager, RequestPrincipal
from server import create_app


def _make_app(tmp_path: Path, **overrides: Any):
    defaults: Dict[str, Any] = dict(
        auto_export_extension=False,
        auto_spawn_tabs=False,
        auto_reset_new_chat=False,
        default_model="gemini-4-carbon",
        api_key="",
        host_reserved_slots=2,
        generated_dir=str(tmp_path / "generated"),
        response_store_dir=str(tmp_path / "responses"),
        distribution_path=str(tmp_path / "generated" / "client_distribution.json"),
    )
    defaults.update(overrides)
    return create_app(BridgeSettings(**defaults))


def test_localhost_keyless_vs_public_tunnel_boundary_and_admin_lockdown(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        # 1) Direct localhost works keylessly and can manage distribution
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 200
        assert client.get("/v1/models").status_code == 200

        created = client.post(
            "/api/distribution/clients",
            json={"name": "室友小张", "max_concurrency": 2, "rpm_limit": 20},
        )
        assert created.status_code == 200
        alice = created.json()["client"]
        alice_key = alice["api_key"]
        assert alice_key.startswith("sk-geb-")

        dist_file = tmp_path / "generated" / "client_distribution.json"
        assert dist_file.exists()
        expected_mode = 0o666 if os.name == "nt" else 0o600
        assert (dist_file.stat().st_mode & 0o777) == expected_mode

        # 2) Public tunnel / reverse proxy headers WITHOUT key -> 401 on /v1/*
        for proxy_headers in (
            {"CF-Connecting-IP": "203.0.113.10", "Host": "ai.example.com"},
            {"X-Forwarded-For": "198.51.100.7"},
            {"Forwarded": "for=198.51.100.8;proto=https"},
            {"Host": "public-bridge.example.org"},
        ):
            unauth = client.get("/v1/models", headers=proxy_headers)
            assert unauth.status_code == 401
            assert unauth.json()["error"]["code"] == "invalid_api_key"

        # 3) Public tunnel WITH valid client key succeeds on /v1/models
        pub_ok = client.get(
            "/v1/models",
            headers={
                "CF-Connecting-IP": "203.0.113.10",
                "Host": "ai.example.com",
                "Authorization": f"Bearer {alice_key}",
            },
        )
        assert pub_ok.status_code == 200

        # 4) Admin & internal bridge routes reject ALL proxied traffic (even with valid client key)
        pub_headers_with_key = {
            "X-Forwarded-For": "203.0.113.10",
            "Authorization": f"Bearer {alice_key}",
        }
        for method, path in (
            ("GET", "/"),
            ("GET", "/api/status"),
            ("GET", "/api/usage"),
            ("GET", "/api/distribution"),
            ("POST", "/api/distribution/clients"),
            ("POST", "/api/slots/reset"),
            ("POST", "/api/bridge/register"),
        ):
            resp = client.request(method, path, headers=pub_headers_with_key, json={})
            assert resp.status_code == 403, f"{method} {path} should block public proxy"
            assert resp.json()["error"]["code"] == "local_admin_only"

        # 5) Even on direct localhost, an external client key cannot access host admin endpoints
        local_client_admin = client.get(
            "/api/distribution",
            headers={"Authorization": f"Bearer {alice_key}"},
        )
        assert local_client_admin.status_code == 403
        assert local_client_admin.json()["error"]["code"] == "client_key_forbidden_on_admin"

        # 6) Browser extension WebSocket rejects proxied connections with 1008
        with pytest.raises(WebSocketDisconnect) as ws_exc:
            with client.websocket_connect(
                "/ws/browser?client_id=attacker",
                headers={"X-Forwarded-For": "203.0.113.99"},
            ):
                pass
        assert ws_exc.value.code == 1008


def test_client_lifecycle_model_whitelist_rotation_and_pause(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        res = client.post(
            "/api/distribution/clients",
            json={
                "name": "受限客户端",
                "allowed_models": ["gemini-4-argon"],
                "max_concurrency": 1,
                "rpm_limit": 10,
            },
        )
        assert res.status_code == 200
        rec = res.json()["client"]
        cid = rec["client_id"]
        key1 = rec["api_key"]

        ext_headers = {
            "X-Forwarded-For": "203.0.113.21",
            "Authorization": f"Bearer {key1}",
        }

        # Model list filtered to allowed model only
        models_resp = client.get("/v1/models", headers=ext_headers)
        assert models_resp.status_code == 200
        model_ids = [m["id"] for m in models_resp.json()["data"]]
        assert set(model_ids) == {"gemini-4-argon", "gemini-4-argon-newchat"}

        # Calling disallowed model fails immediately with 403 model_not_allowed
        denied = client.post(
            "/v1/chat/completions",
            headers=ext_headers,
            json={"model": "gemini-4-carbon", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "model_not_allowed"

        # Pause single client -> 403 client_disabled
        client.patch(f"/api/distribution/clients/{cid}", json={"enabled": False})
        paused = client.get("/v1/models", headers=ext_headers)
        assert paused.status_code == 403
        assert paused.json()["error"]["code"] == "client_disabled"
        client.patch(f"/api/distribution/clients/{cid}", json={"enabled": True})

        # Pause global external access -> external gets 403 while localhost still works
        client.patch("/api/distribution/policy", json={"external_access_enabled": False})
        assert client.get("/v1/models", headers=ext_headers).status_code == 403
        assert client.get("/v1/models").status_code == 200
        client.patch("/api/distribution/policy", json={"external_access_enabled": True})

        # Key rotation invalidates old key immediately
        rot = client.post(f"/api/distribution/clients/{cid}/rotate-key")
        assert rot.status_code == 200
        key2 = rot.json()["client"]["api_key"]
        assert key2 != key1
        assert client.get("/v1/models", headers=ext_headers).status_code == 401
        assert client.get(
            "/v1/models",
            headers={"X-Forwarded-For": "203.0.113.21", "Authorization": f"Bearer {key2}"},
        ).status_code == 200

        # Revoke/delete client
        assert client.delete(f"/api/distribution/clients/{cid}").status_code == 200
        assert client.get(
            "/v1/models",
            headers={"X-Forwarded-For": "203.0.113.21", "Authorization": f"Bearer {key2}"},
        ).status_code == 401


def test_multi_client_and_host_zero_crosstalk_and_artifact_isolation(tmp_path: Path) -> None:
    app = _make_app(tmp_path, host_reserved_slots=1)
    dispatched: List[Dict[str, Any]] = []

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-multi") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json(
            {
                "type": "tab_status",
                "max_parallel_tabs": 4,
                "tabs": [
                    {"tab_id": 101, "tab_url": "https://vertexaisearch.cloud.google.com/u/1/home/cid/c1/r/session/s101", "ready": True},
                    {"tab_id": 102, "tab_url": "https://vertexaisearch.cloud.google.com/u/1/home/cid/c1/r/session/s102", "ready": True},
                    {"tab_id": 103, "tab_url": "https://vertexaisearch.cloud.google.com/u/1/home/cid/c1/r/session/s103", "ready": True},
                ],
            }
        )

        stop_evt = threading.Event()

        def ws_driver() -> None:
            while not stop_evt.is_set():
                try:
                    msg = ws.receive_json()
                except Exception:
                    break
                if msg.get("type") == "execute_prompt":
                    dispatched.append(msg)
                    tab_id = msg.get("tab_id")
                    ws.send_json(
                        {
                            "type": "request_complete",
                            "request_id": msg["request_id"],
                            "tab_id": tab_id,
                            "session_url": f"https://vertexaisearch.cloud.google.com/u/1/home/cid/c1/r/session/s{tab_id}",
                            "reply": f"reply-from-tab-{tab_id}",
                        }
                    )

        threading.Thread(target=ws_driver, daemon=True).start()
        try:
            for _ in range(20):
                if len(client.get("/api/status").json()["hub"]["worker_slots"]) == 3:
                    break
                time.sleep(0.02)

            ca = client.post("/api/distribution/clients", json={"name": "Alice"}).json()["client"]
            cb = client.post("/api/distribution/clients", json={"name": "Bob"}).json()["client"]
            hdr_a = {"X-Forwarded-For": "198.51.100.1", "X-Forwarded-Host": "share.example.com", "X-Forwarded-Proto": "https", "Authorization": f"Bearer {ca['api_key']}"}
            hdr_b = {"X-Forwarded-For": "198.51.100.2", "X-Forwarded-Host": "share.example.com", "X-Forwarded-Proto": "https", "Authorization": f"Bearer {cb['api_key']}"}

            # 1) Host, Alice, and Bob all send IDENTICAL first message AND IDENTICAL X-Session-Id
            same_msg = [{"role": "user", "content": "你好，记住我的专属项目代号"}]
            r_host = client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "room-alpha"},
                json={"model": "gemini-4-carbon", "messages": same_msg},
            )
            r_alice = client.post(
                "/v1/chat/completions",
                headers={**hdr_a, "X-Session-Id": "room-alpha"},
                json={"model": "gemini-4-carbon", "messages": same_msg},
            )
            r_bob = client.post(
                "/v1/chat/completions",
                headers={**hdr_b, "X-Session-Id": "room-alpha"},
                json={"model": "gemini-4-carbon", "messages": same_msg},
            )
            assert r_host.status_code == 200
            assert r_alice.status_code == 200
            assert r_bob.status_code == 200

            used_tabs = [cmd["tab_id"] for cmd in dispatched[:3]]
            assert len(set(used_tabs)) == 3, f"Host, Alice, and Bob must land on 3 separate tabs, got {used_tabs}"

            # Follow-up turn from Alice reuses ONLY Alice's tab without touching Host or Bob
            alice_tab = used_tabs[1]
            r_alice_2 = client.post(
                "/v1/chat/completions",
                headers={**hdr_a, "X-Session-Id": "room-alpha"},
                json={
                    "model": "gemini-4-carbon",
                    "messages": [
                        same_msg[0],
                        {"role": "assistant", "content": f"reply-from-tab-{alice_tab}"},
                        {"role": "user", "content": "继续第二步"},
                    ],
                },
            )
            assert r_alice_2.status_code == 200
            assert dispatched[3]["tab_id"] == alice_tab

            # 2) File upload isolation & local filesystem path redaction
            up_a = client.post(
                "/v1/files",
                headers=hdr_a,
                files={"file": ("secret.txt", b"alice-confidential-notes", "text/plain")},
            )
            assert up_a.status_code == 200
            up_meta = up_a.json()
            file_id = up_meta["id"]
            assert "path" not in up_meta, "External client responses must never expose host local disk paths"

            assert client.get(f"/v1/files/{file_id}", headers=hdr_a).status_code == 200
            assert client.get(f"/v1/files/{file_id}/content", headers=hdr_a).status_code == 200
            assert client.get(f"/v1/files/{file_id}", headers=hdr_b).status_code == 404
            assert client.get(f"/v1/files/{file_id}/content", headers=hdr_b).status_code == 404
            assert client.delete(f"/v1/files/{file_id}", headers=hdr_b).status_code == 404

            # 3) Stored /v1/responses isolation across clients
            resp_a = client.post(
                "/v1/responses",
                headers=hdr_a,
                json={"model": "gemini-4-carbon", "input": "Alice private response", "store": True},
            )
            assert resp_a.status_code == 200
            resp_id = resp_a.json()["id"]
            assert client.get(f"/v1/responses/{resp_id}", headers=hdr_a).status_code == 200
            assert client.get(f"/v1/responses/{resp_id}", headers=hdr_b).status_code == 404
            hijack = client.post(
                "/v1/responses",
                headers=hdr_b,
                json={"model": "gemini-4-carbon", "input": "steal", "previous_response_id": resp_id},
            )
            assert hijack.status_code in {400, 404}
            assert "not found" in hijack.text.lower() or "不存在" in hijack.text or "previous_response" in hijack.text.lower()

            # Verify ledger & status snapshot carry tenant attribution for dashboard rendering
            st = client.get("/api/status").json()
            recent_tenants = {r.get("tenant_name") for r in st["usage"]["recent_requests"]}
            assert "本机直连" in recent_tenants
            assert "客户端 · Alice" in recent_tenants
            assert "客户端 · Bob" in recent_tenants
        finally:
            stop_evt.set()


def test_host_reserved_slots_never_starved_by_external_burst(tmp_path: Path) -> None:
    app = _make_app(tmp_path, host_reserved_slots=2)
    app.state.distribution.update_policy(host_reserved_slots=2, external_queue_timeout_seconds=1.2)
    app.state.distribution.external_queue_timeout_seconds = 1.1

    ext_entered = threading.Event()
    release_ext = threading.Event()
    active_lock = threading.Lock()
    ext_in_browser = 0

    with TestClient(app) as client, client.websocket_connect("/ws/browser?client_id=ext-pool") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json(
            {
                "type": "tab_status",
                "max_parallel_tabs": 4,
                "tabs": [
                    {"tab_id": tid, "tab_url": f"https://vertexaisearch.cloud.google.com/u/1/home/cid/p/r/session/{tid}", "ready": True}
                    for tid in (201, 202, 203, 204)
                ],
            }
        )

        stop_evt = threading.Event()

        def ws_driver() -> None:
            nonlocal ext_in_browser
            while not stop_evt.is_set():
                try:
                    msg = ws.receive_json()
                except Exception:
                    break
                if msg.get("type") != "execute_prompt":
                    continue
                prompt_text = msg.get("prompt") or ""
                req_id = msg["request_id"]
                tab_id = msg.get("tab_id")
                if "SLOW_EXTERNAL" in prompt_text:
                    with active_lock:
                        ext_in_browser += 1
                        if ext_in_browser >= 2:
                            ext_entered.set()

                    def finish_later(rid: str = req_id, tid: Any = tab_id) -> None:
                        release_ext.wait(timeout=5.0)
                        ws.send_json({"type": "request_complete", "request_id": rid, "tab_id": tid, "reply": "ext-done"})

                    threading.Thread(target=finish_later, daemon=True).start()
                else:
                    ws.send_json({"type": "request_complete", "request_id": req_id, "tab_id": tab_id, "reply": "host-fast-ok"})

        threading.Thread(target=ws_driver, daemon=True).start()
        try:
            for _ in range(20):
                if len(client.get("/api/status").json()["hub"]["worker_slots"]) == 4:
                    break
                time.sleep(0.02)

            c_heavy = client.post(
                "/api/distribution/clients",
                json={"name": "HeavyUser", "max_concurrency": 5, "rpm_limit": 60},
            ).json()["client"]
            hdr_ext = {"X-Forwarded-For": "198.51.100.50", "Authorization": f"Bearer {c_heavy['api_key']}"}

            def call_ext(idx: int):
                return client.post(
                    "/v1/chat/completions",
                    headers=hdr_ext,
                    json={"model": "gemini-4-carbon", "messages": [{"role": "user", "content": f"SLOW_EXTERNAL_{idx}"}]},
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                f1 = pool.submit(call_ext, 1)
                f2 = pool.submit(call_ext, 2)
                assert ext_entered.wait(timeout=3.0), "Two external requests should occupy the external parallel cap"

                f3 = pool.submit(call_ext, 3)

                # Meanwhile, Host sends a request and immediately gets one of its 2 reserved tabs!
                t0 = time.monotonic()
                host_res = client.post(
                    "/v1/chat/completions",
                    json={"model": "gemini-4-carbon", "messages": [{"role": "user", "content": "HOST_URGENT"}]},
                )
                host_elapsed = time.monotonic() - t0
                assert host_res.status_code == 200
                assert host_res.json()["choices"][0]["message"]["content"] == "host-fast-ok"
                assert host_elapsed < 1.0, f"Host should never queue behind external load (took {host_elapsed:.2f}s)"

                r3 = f3.result(timeout=4.0)
                assert r3.status_code == 429
                assert r3.json()["error"]["code"] == "concurrency_timeout"

                release_ext.set()
                assert f1.result(timeout=4.0).status_code == 200
                assert f2.result(timeout=4.0).status_code == 200
        finally:
            release_ext.set()
            stop_evt.set()


def test_fair_scheduler_rpm_and_daily_quotas(tmp_path: Path) -> None:
    dm = DistributionManager(storage_path=tmp_path / "dist.json", default_host_reserved_slots=1)
    ca = dm.create_client(name="ClientA", max_concurrency=3, rpm_limit=50)
    cb = dm.create_client(name="ClientB", max_concurrency=3, rpm_limit=50)
    pa = RequestPrincipal(tenant_id=f"client:{ca['client_id']}", client_id=ca["client_id"], name="ClientA", is_host=False, is_direct_local=False)
    pb = RequestPrincipal(tenant_id=f"client:{cb['client_id']}", client_id=cb["client_id"], name="ClientB", is_host=False, is_direct_local=False)

    async def check_fair_order() -> List[str]:
        admission_order: List[str] = []
        a1_entered = asyncio.Event()
        a2_entered = asyncio.Event()
        release_a2 = asyncio.Event()
        release_a1 = asyncio.Event()

        async def hold_a1() -> None:
            async with dm.acquire_execution_permit(pa, capacity_provider=lambda: 3):
                a1_entered.set()
                await release_a1.wait()

        async def hold_a2() -> None:
            async with dm.acquire_execution_permit(pa, capacity_provider=lambda: 3):
                a2_entered.set()
                await release_a2.wait()

        async def waiter(label: str, principal: RequestPrincipal) -> None:
            async with dm.acquire_execution_permit(principal, capacity_provider=lambda: 3):
                admission_order.append(label)

        t_a1 = asyncio.create_task(hold_a1())
        t_a2 = asyncio.create_task(hold_a2())
        await a1_entered.wait()
        await a2_entered.wait()

        # External cap (2) is full (A1 + A2). Enqueue A3 first, then B1.
        t_a3 = asyncio.create_task(waiter("A3", pa))
        await asyncio.sleep(0.03)
        t_b1 = asyncio.create_task(waiter("B1", pb))
        await asyncio.sleep(0.03)

        # Release A2 while A1 remains in-flight: B has 0 in-flight vs A's 1 in-flight -> B1 wins first!
        release_a2.set()
        await asyncio.gather(t_a2, t_b1, t_a3)
        release_a1.set()
        await t_a1
        return admission_order

    order = asyncio.run(check_fair_order())
    assert order == ["B1", "A3"], f"Expected B1 (0 in-flight) to win ahead of A3 (1 in-flight), got {order}"

    # 2) RPM limit & Retry-After
    c_rpm = dm.create_client(name="RpmLimited", max_concurrency=2, rpm_limit=2, daily_request_limit=2)
    p_rpm = RequestPrincipal(
        tenant_id=f"client:{c_rpm['client_id']}",
        client_id=c_rpm["client_id"],
        name="RpmLimited",
        is_host=False,
        is_direct_local=False,
    )

    async def consume_once() -> None:
        async with dm.acquire_execution_permit(p_rpm, capacity_provider=lambda: 5):
            pass

    asyncio.run(consume_once())
    asyncio.run(consume_once())
    with pytest.raises(Exception) as rpm_exc:
        asyncio.run(consume_once())
    assert getattr(rpm_exc.value, "code", None) == "rate_limit_exceeded"
    assert getattr(rpm_exc.value, "retry_after", 0) >= 1

    # 3) Daily request quota enforcement
    c_day = dm.create_client(name="DailyLimited", max_concurrency=2, rpm_limit=50, daily_request_limit=1, daily_token_limit=500)
    p_day = RequestPrincipal(
        tenant_id=f"client:{c_day['client_id']}",
        client_id=c_day["client_id"],
        name="DailyLimited",
        is_host=False,
        is_direct_local=False,
    )
    dm.record_usage(p_day, prompt_tokens=100, completion_tokens=50)

    async def try_over_daily() -> None:
        async with dm.acquire_execution_permit(p_day, capacity_provider=lambda: 5):
            pass

    with pytest.raises(Exception) as day_exc:
        asyncio.run(try_over_daily())
    assert getattr(day_exc.value, "code", None) == "daily_quota_exceeded"

"""Tests for the public availability monitor (/status, /status/ping, /api/public/status)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from bridge_hub import BrowserClientState
from config import BridgeSettings
from public_status import PublicStatusMonitor, build_external_speedtest_urls
from server import create_app


def test_public_status_page_javascript_and_proxy_access(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate public status JavaScript"
    settings = BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path))
    app = create_app(settings)

    proxy_headers = {
        "CF-Connecting-IP": "198.51.100.42",
        "X-Forwarded-Proto": "https",
        "Host": "demo-status.trycloudflare.com",
    }

    with TestClient(app) as client:
        # Admin console root remains locked to 403 over public proxy
        root_res = client.get("/", headers=proxy_headers)
        assert root_res.status_code == 403

        # Public status page is reachable without any API key over public proxy
        page_res = client.get("/status", headers=proxy_headers)
        assert page_res.status_code == 200
        assert "全国各省可用性与三网延迟地图" in page_res.text
        assert "可用性, 7天" in page_res.text
        assert "tcptest.cn" not in page_res.text

        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", page_res.text, re.S | re.I)
        assert scripts, "Public status page must include interactive script"
        for script in scripts:
            proc = subprocess.run(
                [node, "--check", "--input-type=commonjs"],
                input=script,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr


def test_public_ping_online_offline_and_zero_privacy_leak(tmp_path: Path) -> None:
    settings = BridgeSettings(
        auto_export_extension=False,
        default_model="gemini-4-carbon",
        generated_dir=str(tmp_path),
    )
    app = create_app(settings)
    proxy_headers = {
        "CF-Connecting-IP": "203.0.113.77",
        "X-Forwarded-Proto": "https",
        "Host": "edge-check.trycloudflare.com",
    }

    with TestClient(app) as client:
        # 1) Before any browser worker connects -> /status/ping returns 503 so external maps show offline
        ping_down = client.get("/status/ping", headers=proxy_headers)
        assert ping_down.status_code == 503
        assert ping_down.json()["online"] is False
        head_down = client.head("/status/ping", headers=proxy_headers)
        assert head_down.status_code == 503

        # 2) Connect a ready browser worker slot -> /status/ping flips to 200 OK
        hub = app.state.hub
        hub._clients["ext-1"] = BrowserClientState(client_id="ext-1", transport="ws")
        slot = hub._upsert_single_slot(
            client_id="ext-1",
            tab_id=101,
            tab_url="https://vertexaisearch.cloud.google.com/u/1/home",
            tab_title="Private Homework Title",
            tab_ready=True,
            active_model="4-carbon",
        )
        ping_up = client.get("/status/ping", headers=proxy_headers)
        assert ping_up.status_code == 200
        assert ping_up.json()["online"] is True
        assert ping_up.json()["busy"] is False

        # Busy state when all slots are occupied
        slot.in_flight_request_id = "req-1"
        ping_busy = client.get("/status/ping", headers=proxy_headers)
        assert ping_busy.status_code == 200
        assert ping_busy.json()["busy"] is True
        slot.in_flight_request_id = None

        # 3) Record sensitive client activity + passive latency + rejected auth noise
        created_client = client.post(
            "/api/distribution/clients",
            json={"name": "VIP-Secret-Customer", "note": "internal-note-xyz"},
        ).json()["client"]
        secret_key = created_client["api_key"]

        ledger = app.state.usage_ledger
        ledger.record_request(
            requested_model="gemini-4-carbon",
            prompt_tokens=120,
            completion_tokens=80,
            duration_ms=4280,
            tenant_id=created_client["client_id"],
            tenant_name="VIP-Secret-Customer",
            client_ip="198.51.100.199",
            session_id="secret-sess-999",
            session_title="TopSecretProject",
            prompt_preview="CONFIDENTIAL_PROMPT_TEXT",
            reply_preview="CONFIDENTIAL_REPLY_TEXT",
        )
        # Client-side 401/429 rejections must NOT turn upstream model status red
        ledger.record_failed_request(
            requested_model="gemini-4-carbon",
            tenant_id="external_unauth",
            tenant_name="未授权外部请求",
            client_ip="192.0.2.88",
            status="forbidden",
            status_code=401,
            error_code="invalid_api_key",
            error_message="bad key",
        )

        snap_res = client.get("/api/public/status", headers=proxy_headers)
        assert snap_res.status_code == 200
        snap = snap_res.json()
        raw_json = json.dumps(snap, ensure_ascii=False)

        # Verify zero leakage of secrets, client names, IPs, prompts, or session titles
        for forbidden in (
            secret_key,
            "VIP-Secret-Customer",
            "internal-note-xyz",
            "198.51.100.199",
            "secret-sess-999",
            "TopSecretProject",
            "CONFIDENTIAL_PROMPT_TEXT",
            "CONFIDENTIAL_REPLY_TEXT",
            "Private Homework Title",
        ):
            assert forbidden not in raw_json

        assert snap["online"] is True
        assert snap["is_public_origin"] is True
        assert snap["probe_url"] == "https://edge-check.trycloudflare.com/status/ping"
        assert "tcptest" not in raw_json
        ext_urls = build_external_speedtest_urls("https://edge-check.trycloudflare.com/status/ping")
        assert snap["itdog_url"] == "https://www.itdog.cn/http/"
        assert snap["chinaz_url"] == ext_urls["chinaz_url"]
        assert snap["boce_url"] == "https://www.boce.com/http/edge-check.trycloudflare.com"

        monitors = {m["id"]: m for m in snap["monitors"]}
        assert list(monitors.keys()) == [
            "gemini-4-carbon",
            "gemini-4-argon",
            "gemini-3.1-pro",
            "gemini-3.8-flash",
        ]
        assert "gemini-enterprise-web" not in monitors
        assert monitors["gemini-4-carbon"]["is_primary"] is True
        assert monitors["gemini-4-carbon"]["chat_latency_ms"] == 4280
        assert monitors["gemini-4-carbon"]["state"] == "up"
        assert len(monitors["gemini-4-carbon"]["ticks"]) == 60


def test_network_map_probe_caching_and_isp_classification(tmp_path: Path, monkeypatch) -> None:
    settings = BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path))
    app = create_app(settings)
    calls = {"count": 0}

    def fake_globalping(public_origin: str, probe_path: str = "/status/ping"):
        calls["count"] += 1
        return {
            "ok": True,
            "measurement_id": f"meas-{calls['count']}",
            "origin": public_origin,
            "probe_url": f"{public_origin}{probe_path}",
            "tested_at": int(time.time()),
            "cache_ttl_sec": 90,
            "summary": {
                "total_nodes": 3,
                "reachable_nodes": 3,
                "reachability_pct": 100.0,
                "telecom_avg_ms": 920,
                "unicom_avg_ms": 788,
                "mobile_avg_ms": 1549,
                "overseas_avg_ms": 776,
            },
            "nodes": [
                {
                    "country": "CN",
                    "city": "Shanghai",
                    "region_zh": "上海",
                    "isp_key": "unicom",
                    "isp_label": "中国联通",
                    "network": "China Unicom Shanghai",
                    "lat": 31.22,
                    "lon": 121.46,
                    "http_code": 200,
                    "state": "fast",
                    "edge_ms": 87,
                    "ttfb_ms": 395,
                    "total_ms": 788,
                }
            ],
        }

    monkeypatch.setattr(
        app.state.public_status_monitor,
        "_execute_globalping_http_probe",
        fake_globalping,
    )

    proxy_headers = {
        "CF-Connecting-IP": "203.0.113.55",
        "X-Forwarded-Proto": "https",
        "Host": "map-demo.trycloudflare.com",
    }

    with TestClient(app) as client:
        # Localhost without active tunnel returns friendly 400
        local_res = client.post("/api/public/network-map")
        assert local_res.status_code == 400
        assert local_res.json()["ok"] is False

        # Public origin runs Globalping once and caches for 90s (external force=1 cannot bypass cache)
        r1 = client.post("/api/public/network-map", headers=proxy_headers)
        assert r1.status_code == 200
        assert r1.json()["cached"] is False
        assert calls["count"] == 1

        r2 = client.post("/api/public/network-map?force=1", headers=proxy_headers)
        assert r2.status_code == 200
        assert r2.json()["cached"] is True
        assert calls["count"] == 1


def test_status_monitor_7d_uptime_persistence(tmp_path: Path) -> None:
    hist_file = tmp_path / "public_status_history.json"
    mon = PublicStatusMonitor(storage_path=hist_file, sample_interval_sec=60)
    base_ts = 1794840000
    for idx in range(9):
        mon.record_sample(online=True, busy=False, latest_chat_ms=3200, now_ts=base_ts + idx * 60, force=True)
    mon.record_sample(online=False, busy=False, now_ts=base_ts + 9 * 60, force=True)

    reloaded = PublicStatusMonitor(storage_path=hist_file, sample_interval_sec=60)
    snap = reloaded.build_public_snapshot(
        online=True,
        busy=False,
        public_origin="https://relay.example.com",
        now_ts=base_ts + 10 * 60,
    )
    # 9 up + 1 down + 1 up on build_public_snapshot = 10/11 = 90.91%
    gw = snap["monitors"][0]
    assert gw["uptime_7d_pct"] == 90.91
    assert len(gw["ticks"]) == 60
import socket
import urllib.error

import tunnel_manager


def test_tunnel_dns_negative_cache_doh_fallback_and_status_consistency(tmp_path: Path, monkeypatch) -> None:
    settings = BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path))
    app = create_app(settings)
    tm = app.state.tunnel_manager
    hub = app.state.hub
    hub._clients["ext-1"] = BrowserClientState(client_id="ext-1", transport="ws")
    hub._upsert_single_slot(
        client_id="ext-1",
        tab_id=1,
        tab_url="https://vertexaisearch.cloud.google.com/u/1/home",
        tab_title="Workspace",
        tab_ready=True,
        active_model="4-carbon",
    )

    with TestClient(app) as client:
        # 1) Local view with tunnel stopped clearly indicates public tunnel is off
        local_closed = client.get("/api/public/status").json()
        assert local_closed["tunnel_state"] == "local_only"
        assert "公网通道未开启" in local_closed["overall_label"]

        # 2) Failed reachability is offline, even when the browser worker remains healthy.
        pub_origin = "https://fresh-quick-node.trycloudflare.com"
        app.state.distribution.update_policy(public_base_url=pub_origin)
        tm._last_probe = {
            "ok": False,
            "tested_at": int(time.time()),
            "public_url": pub_origin,
            "latency_ms": 3882,
            "health_ok": False,
            "detail": "公网连接异常",
        }
        local_failed = client.get("/api/public/status").json()
        assert local_failed["tunnel_state"] == "down"
        assert local_failed["status"] == "offline"
        assert local_failed["online"] is False
        assert local_failed["busy"] is False
        assert local_failed["overall_label"] == "本机服务正常 · 公网连接异常"
        assert local_failed["server_probe_ms"] is None
        assert local_failed["gateway_ticks"][-1]["state"] == "down"
        assert all(item["state"] == "down" for item in local_failed["monitors"])

        # 3) Real external traffic arriving over that public tunnel clears stale failed _last_probe
        ext_headers = {
            "CF-Connecting-IP": "198.51.100.88",
            "X-Forwarded-Proto": "https",
            "Host": "fresh-quick-node.trycloudflare.com",
        }
        ext_snap = client.get("/api/public/status", headers=ext_headers).json()
        assert ext_snap["tunnel_state"] == "up"
        assert ext_snap["overall_label"] == "服务运行正常"
        assert tm.snapshot()["last_probe"] is None

        # 4) stop() always clears _last_probe
        tm._last_probe = {"ok": False, "detail": "stale"}
        tm.stop(clear_public_url=True)
        assert tm.snapshot()["last_probe"] is None

    # 5) _http_get_with_doh_fallback recovers when OS DNS returns [Errno 8] NXDOMAIN cache
    def broken_urlopen(*args, **kwargs):
        raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known"))

    monkeypatch.setattr(tunnel_manager.urllib.request, "urlopen", broken_urlopen)
    monkeypatch.setattr(tunnel_manager, "_resolve_host_via_doh", lambda host, timeout=2.5: "104.16.132.229")

    class FakeResp:
        status = 200

        def read(self) -> bytes:
            return b'{"status":"ok","browser_connected":true}'

    class FakePinnedConn:
        def __init__(self, host, *, pinned_ip, port=443, timeout=8.0, context=None):
            assert host == "fresh-quick-node.trycloudflare.com"
            assert pinned_ip == "104.16.132.229"

        def request(self, method, path_q, headers=None):
            assert method == "GET"

        def getresponse(self):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(tunnel_manager, "_PinnedIPHTTPSConnection", FakePinnedConn)
    cache = {}
    code, body = tunnel_manager._http_get_with_doh_fallback(
        "https://fresh-quick-node.trycloudflare.com/health",
        headers={"Accept": "application/json"},
        pinned_ip_cache=cache,
    )
    assert code == 200
    assert '"status":"ok"' in body
    assert cache["fresh-quick-node.trycloudflare.com"] == "104.16.132.229"

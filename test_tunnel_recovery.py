"""Wake/network recovery without replacing healthy or merely offline tunnels."""

import asyncio
import json
import socket
import time
import urllib.error

import pytest

import tunnel_manager as tunnels


OLD_URL = "https://old-route.trycloudflare.com"
NEW_URL = "https://new-route.trycloudflare.com"


def managed_tunnel(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnels, "_is_cloudflared_pid_alive", lambda pid: True)
    tm = tunnels.PublicTunnelManager(state_path=tmp_path / "tunnel.json")
    tm._pid = 123
    tm._active_url = OLD_URL
    tm._started_at = int(time.time()) - 3600
    return tm


@pytest.mark.parametrize("failure_kind", ["dns", "tunnel_disconnected"])
def test_self_check_recovers_stale_live_process_and_verifies_new_address(tmp_path, monkeypatch, failure_kind):
    tm = managed_tunnel(tmp_path, monkeypatch)
    calls = []
    synced = []
    tm._on_url_changed = synced.append
    monkeypatch.setattr(tunnels, "_quick_hostname_expired", lambda host: True)

    def http_get(url, **kwargs):
        calls.append(url)
        if url.startswith(OLD_URL):
            if failure_kind == "dns":
                raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
            return 530, "error code: 1033"
        if url.endswith("/health"):
            return 200, '{"status":"ok"}'
        if url.endswith("/api/status"):
            return 403, "{}"
        return (200 if "Authorization" in kwargs["headers"] else 401), "{}"

    monkeypatch.setattr(tunnels, "_http_get_with_doh_fallback", http_get)
    monkeypatch.setattr(tunnels.time, "sleep", lambda seconds: None)

    async def restart(**kwargs):
        assert kwargs == {"restart": True}
        tm._pid = 456
        tm._active_url = NEW_URL
        tm._started_at = int(time.time())
        tm._on_url_changed(NEW_URL)
        return {"active_url": NEW_URL}

    monkeypatch.setattr(tm, "start", restart)
    result = asyncio.run(tm.probe_and_recover(OLD_URL, sample_api_key="test-key", can_restart=lambda: True))
    assert result["ok"] is True
    assert result["recovery_attempted"] is True
    assert result["url_changed"] is True
    assert result["public_url"] == NEW_URL
    assert result["client_key_verified"] is True
    assert "新地址" in result["detail"]
    assert synced == [NEW_URL]
    assert tm._last_probe == result
    assert calls.count(OLD_URL + "/health") == 5


@pytest.mark.parametrize("case", ["offline", "busy", "fresh", "custom", "healthy", "external_traffic", "admin_stopped", "cooldown"])
def test_self_check_does_not_replace_connection_without_safe_expiry_evidence(tmp_path, monkeypatch, case):
    tm = managed_tunnel(tmp_path, monkeypatch)
    if case == "fresh":
        tm._started_at = int(time.time())
    if case == "cooldown":
        tm._last_recovery_at = time.monotonic()
    monkeypatch.setattr(tunnels, "_quick_hostname_expired", lambda host: case != "offline")

    async def probe(public_url, **kwargs):
        if case == "external_traffic":
            tm.note_verified_external_traffic(OLD_URL)
        if case == "admin_stopped":
            tm._pid = 0
            tm._active_url = ""
        return {
            "ok": case == "healthy", "health_ok": case == "healthy",
            "public_url": public_url, "failure_kind": "dns", "detail": "公网域名暂时无法解析",
        }

    async def forbidden_restart(**kwargs):
        pytest.fail("Do not restart a healthy, busy, changed or unverified connection")

    monkeypatch.setattr(tm, "probe_public_reachability", probe)
    monkeypatch.setattr(tm, "start", forbidden_restart)
    result = asyncio.run(tm.probe_and_recover(
        "https://custom.example.com" if case == "custom" else OLD_URL,
        can_restart=lambda: case != "busy",
    ))
    assert not result.get("recovery_attempted")
    if case == "busy":
        assert "任务" in result["detail"]


def test_named_tunnel_reconnect_retains_fixed_domain(tmp_path, monkeypatch):
    tm = managed_tunnel(tmp_path, monkeypatch)
    tm.mode = "named"
    tm._active_url = "https://fixed.example.com"
    url = tm._active_url
    attempts = []

    async def probe(public_url, **kwargs):
        return {"health_ok": bool(attempts), "ok": bool(attempts), "public_url": public_url,
                "failure_kind": "tunnel_disconnected", "detail": "连通正常" if attempts else "连接断开"}

    async def restart(**kwargs):
        attempts.append(kwargs)
        return {"active_url": url}

    monkeypatch.setattr(tm, "probe_public_reachability", probe)
    monkeypatch.setattr(tm, "start", restart)
    result = asyncio.run(tm.probe_and_recover(url, can_restart=lambda: True))
    assert result["ok"] is True
    assert result["url_changed"] is False
    assert "原公网通道" in result["detail"]
    assert len(attempts) == 1


def test_reconnect_is_not_reported_as_success_if_second_probe_fails(tmp_path, monkeypatch):
    tm = managed_tunnel(tmp_path, monkeypatch)
    attempts = []

    async def probe(public_url, **kwargs):
        return {"ok": False, "health_ok": False, "public_url": public_url,
                "failure_kind": "tunnel_disconnected", "detail": "连接仍然断开"}

    async def restart(**kwargs):
        attempts.append(kwargs)
        return {"active_url": OLD_URL}

    monkeypatch.setattr(tm, "probe_public_reachability", probe)
    monkeypatch.setattr(tm, "start", restart)

    async def two_checks():
        first = await tm.probe_and_recover(OLD_URL, can_restart=lambda: True)
        second = await tm.probe_and_recover(OLD_URL, can_restart=lambda: True)
        return first, second

    first, second = asyncio.run(two_checks())
    assert first["ok"] is False
    assert "仍未连通" in first["detail"]
    assert "已重新连接原公网通道。" not in first["detail"]
    assert len(attempts) == 1
    assert not second.get("recovery_attempted")


def test_admin_probe_defers_recovery_while_requests_are_queued(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from config import BridgeSettings
    from server import create_app

    app = create_app(BridgeSettings(auto_export_extension=False, generated_dir=str(tmp_path)))
    tm = app.state.tunnel_manager
    observed_idle = []

    async def probe(public_url, *, sample_api_key, can_restart):
        observed_idle.append(can_restart())
        return {"ok": False, "health_ok": False}

    monkeypatch.setattr(tm, "probe_and_recover", probe)
    with TestClient(app) as client:
        for in_flight, queued in [(0, 0), (1, 0), (0, 1)]:
            with monkeypatch.context() as patch:
                patch.setattr(app.state.hub, "status_snapshot", lambda: {
                    "in_flight_requests": in_flight, "queued_requests": queued,
                })
                response = client.post("/api/tunnel/probe", json={})
                assert response.status_code == 200
                assert "distribution" in response.json()
    assert observed_idle == [True, False, False]


@pytest.mark.parametrize("statuses,expected", [([3, 3], True), ([3, 0], False), ([0], False), ([3, None], False)])
def test_expiry_requires_matching_public_dns_responses(monkeypatch, statuses, expected):
    replies = iter(statuses)

    class Reply:
        status = 200

        def __init__(self, dns_status):
            self.dns_status = dns_status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"Status": self.dns_status}).encode()

    def urlopen(req, **kwargs):
        status = next(replies)
        if status is None:
            raise TimeoutError("offline")
        return Reply(status)

    monkeypatch.setattr(tunnels.urllib.request, "urlopen", urlopen)
    assert tunnels._quick_hostname_expired("old-route.trycloudflare.com") is expected
    assert tunnels._quick_hostname_expired("fixed.example.com") is False


def test_windows_dns_failure_and_cloudflare_disconnect_are_not_domain_propagation():
    for exc, code in [("[Errno 11001] getaddrinfo failed", 0), ("HTTP 530", 530),
                      ("[Errno -3] Temporary failure in name resolution", 0)]:
        detail = tunnels._humanize_probe_failure(exc, code)
        assert "新公网域名" not in detail
        assert "同步" not in detail
        assert "3–5" not in detail


def test_stale_pinned_edge_ip_is_discarded_after_network_failure(monkeypatch):
    class BrokenPinnedConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            raise ConnectionResetError("network changed during sleep")

        def close(self):
            pass

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"status":"ok"}'

    monkeypatch.setattr(tunnels, "_PinnedIPHTTPSConnection", BrokenPinnedConnection)
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", lambda *args, **kwargs: Reply())
    cache = {"old-route.trycloudflare.com": "104.16.132.229"}
    assert tunnels._http_get_with_doh_fallback(OLD_URL + "/health", headers={}, pinned_ip_cache=cache)[0] == 200
    assert cache == {}

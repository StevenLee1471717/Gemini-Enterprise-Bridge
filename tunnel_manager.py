"""Managed free public tunnel controller (Cloudflare Quick Tunnel / Named Tunnel & reachability probe)."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from platform_compat import (
    IS_WINDOWS,
    atomic_write_json,
    best_effort_chmod,
    find_executable,
    is_pid_alive,
    subprocess_hidden_kwargs,
    terminate_pid_gracefully,
)

logger = logging.getLogger("gemini_bridge.tunnel")

_TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
_POSIX_CLOUDFLARED_PATHS = (
    "/opt/homebrew/bin/cloudflared",
    "/usr/local/bin/cloudflared",
    "/usr/bin/cloudflared",
)
_WINDOWS_CLOUDFLARED_PATHS = (
    r"%ProgramFiles%\cloudflared\cloudflared.exe",
    r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe",
    r"%USERPROFILE%\scoop\shims\cloudflared.exe",
    r"%ProgramData%\chocolatey\bin\cloudflared.exe",
    r"C:\cloudflared\cloudflared.exe",
)
_POSIX_TAILSCALE_PATHS = (
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)
_WINDOWS_TAILSCALE_PATHS = (
    r"%ProgramFiles%\Tailscale\tailscale.exe",
    r"%ProgramFiles(x86)%\Tailscale\tailscale.exe",
    r"%LOCALAPPDATA%\Tailscale\tailscale.exe",
)


def find_cloudflared_binary() -> Optional[str]:
    return find_executable(
        "cloudflared",
        posix_candidates=_POSIX_CLOUDFLARED_PATHS,
        windows_candidates=_WINDOWS_CLOUDFLARED_PATHS,
    )


def find_tailscale_binary() -> Optional[str]:
    return find_executable(
        "tailscale",
        posix_candidates=_POSIX_TAILSCALE_PATHS,
        windows_candidates=_WINDOWS_TAILSCALE_PATHS,
    )


def _is_cloudflared_pid_alive(pid: int) -> bool:
    return is_pid_alive(pid, expected_name_substr="cloudflared")


def _normalize_https_origin(raw: str) -> str:
    cleaned = (raw or "").strip().rstrip("/")
    if cleaned.endswith("/v1"):
        cleaned = cleaned[:-3].rstrip("/")
    return cleaned


_DOH_JSON_ENDPOINTS: tuple[str, ...] = (
    "https://1.1.1.1/dns-query",
    "https://223.5.5.5/resolve",
)


def _is_dns_resolution_error(exc: BaseException) -> bool:
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), socket.gaierror):
        return True
    msg = str(exc or "").lower()
    dns_markers = (
        "nodename nor servname",
        "name or service not known",
        "getaddrinfo failed",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "errno 8",
        "errno -2",
        "errno -3",
        "errno 11001",
    )
    return any(m in msg for m in dns_markers)


def _resolve_host_via_doh(hostname: str, *, timeout: float = 2.5) -> Optional[str]:
    """Resolve A record via public DoH IP literals without touching or poisoning OS DNS cache."""
    host_clean = (hostname or "").strip().rstrip(".")
    if not host_clean:
        return None
    try:
        ipaddress.ip_address(host_clean)
        return host_clean
    except ValueError:
        pass

    ctx = ssl.create_default_context()
    q = urllib.parse.urlencode({"name": host_clean, "type": "A"})
    for endpoint in _DOH_JSON_ENDPOINTS:
        req = urllib.request.Request(
            f"{endpoint}?{q}",
            headers={
                "Accept": "application/dns-json",
                "User-Agent": "GeminiEnterpriseBridge-DoH/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if int(resp.status) != 200:
                    continue
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if not isinstance(payload, dict) or int(payload.get("Status", -1)) != 0:
                continue
            for ans in payload.get("Answer") or []:
                if not isinstance(ans, dict) or int(ans.get("type") or 0) != 1:
                    continue
                candidate = str(ans.get("data") or "").strip()
                try:
                    ip_obj = ipaddress.ip_address(candidate)
                    if not ip_obj.is_loopback and not ip_obj.is_private and not ip_obj.is_unspecified:
                        return candidate
                except ValueError:
                    continue
        except Exception:
            continue
    return None


class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection dialing a DoH-resolved edge IP while preserving strict TLS SNI & hostname check."""

    def __init__(
        self,
        host: str,
        *,
        pinned_ip: str,
        port: int = 443,
        timeout: float = 8.0,
        context: Optional[ssl.SSLContext] = None,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        if self._tunnel_host:
            self.sock = raw_sock
            self._tunnel()
            server_host = self._tunnel_host
        else:
            server_host = self.host
        ctx = self._context or ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=server_host)


def _http_get_with_doh_fallback(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: float = 8.0,
    ctx: Optional[ssl.SSLContext] = None,
    pinned_ip_cache: Optional[Dict[str, str]] = None,
) -> tuple[int, str]:
    """Perform GET via standard urllib, falling back to DoH + strict SNI if OS DNS has stale NXDOMAIN."""
    ssl_ctx = ctx or ssl.create_default_context()
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").strip()
    scheme = (parsed.scheme or "https").lower()
    cache = pinned_ip_cache if pinned_ip_cache is not None else {}

    def _via_pinned_ip(edge_ip: str) -> tuple[int, str]:
        port = parsed.port or 443
        path_q = parsed.path or "/"
        if parsed.query:
            path_q = f"{path_q}?{parsed.query}"
        req_headers = dict(headers)
        req_headers.setdefault("Host", parsed.netloc or hostname)
        req_headers.setdefault("Connection", "close")
        conn = _PinnedIPHTTPSConnection(
            hostname,
            pinned_ip=edge_ip,
            port=port,
            timeout=timeout,
            context=ssl_ctx,
        )
        try:
            conn.request("GET", path_q, headers=req_headers)
            resp = conn.getresponse()
            body_text = resp.read().decode("utf-8", errors="ignore")
            return int(resp.status), body_text
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if scheme == "https" and hostname and hostname in cache:
        try:
            return _via_pinned_ip(cache[hostname])
        except (OSError, http.client.HTTPException):
            cache.pop(hostname, None)

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return int(resp.status), raw
    except urllib.error.HTTPError as he:
        try:
            err_body = he.read().decode("utf-8", errors="ignore")
        except Exception:
            err_body = ""
        return int(he.code), err_body
    except Exception as exc:
        if scheme == "https" and hostname and _is_dns_resolution_error(exc):
            doh_ip = _resolve_host_via_doh(hostname, timeout=min(3.0, timeout))
            if doh_ip:
                cache[hostname] = doh_ip
                return _via_pinned_ip(doh_ip)
        raise


def _humanize_probe_failure(raw_exc: str, status_code: int) -> str:
    low = (raw_exc or "").lower()
    if _is_dns_resolution_error(RuntimeError(raw_exc)):
        return "公网域名暂时无法解析，可能是休眠后网络尚未恢复或临时地址已失效；请检查网络后重新自检"
    if status_code == 530:
        return "Cloudflare 暂时无法连接本机公网通道（HTTP 530），可能是休眠或断网后连接尚未恢复"
    if status_code in {502, 503}:
        return f"隧道边缘暂未就绪（HTTP {status_code}），请稍候重试"
    if "timed out" in low or "timeout" in low:
        return "公网探测连接超时，请检查本机网络后重试"
    if status_code > 0:
        return f"公网地址暂未就绪（HTTP {status_code}），请稍候再测"
    return f"公网地址暂未连通（{raw_exc or '连接未就绪'}），请等 3 秒后再测"


def _quick_hostname_expired(hostname: str) -> bool:
    """Only treat matching NXDOMAIN replies as expiry, never a local DNS/network failure."""
    if not hostname.endswith(".trycloudflare.com"):
        return False
    query = urllib.parse.urlencode({"name": hostname, "type": "A"})
    for endpoint in _DOH_JSON_ENDPOINTS:
        req = urllib.request.Request(
            f"{endpoint}?{query}", headers={"Accept": "application/dns-json"}, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0, context=ssl.create_default_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if resp.status != 200 or not isinstance(payload, dict) or payload.get("Status") != 3:
                    return False
        except Exception:
            return False
    return True


class PublicTunnelManager:
    """Manages a local cloudflared process and verifies external public reachability."""

    def __init__(
        self,
        *,
        target_port: int = 8000,
        state_path: Optional[str | Path] = None,
        on_url_changed: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.target_port = int(target_port)
        self._state_path = Path(state_path) if state_path else None
        self._log_path = (
            self._state_path.with_name("cloudflared_tunnel.log")
            if self._state_path is not None
            else Path(tempfile.gettempdir()) / f"geb_cloudflared_{self.target_port}.log"
        )
        self._on_url_changed = on_url_changed
        self._lock = threading.RLock()

        self.mode: str = "quick"  # quick | named
        self.auto_sync_public_url: bool = True
        self.named_tunnel_token: str = ""
        self.named_public_url: str = ""

        self._pid: int = 0
        self._active_url: str = ""
        self._started_at: int = 0
        self._last_error: str = ""
        self._last_probe: Optional[Dict[str, Any]] = None
        self._recovery_lock = asyncio.Lock()
        self._last_recovery_at: float = 0.0
        self._last_external_traffic_at: float = 0.0

        if self._state_path is not None:
            self._load()

    # ------------------------------------------------------------------
    # Persistence & Process Adoption
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取隧道状态文件失败 (%s): %s", self._state_path, exc)
            return
        if not isinstance(raw, dict):
            return
        if raw.get("mode") in {"quick", "named"}:
            self.mode = str(raw["mode"])
        if "auto_sync_public_url" in raw:
            self.auto_sync_public_url = bool(raw["auto_sync_public_url"])
        if isinstance(raw.get("named_tunnel_token"), str):
            self.named_tunnel_token = raw["named_tunnel_token"].strip()
        if isinstance(raw.get("named_public_url"), str):
            self.named_public_url = _normalize_https_origin(raw["named_public_url"])

        saved_pid = int(raw.get("pid") or 0)
        saved_port = int(raw.get("target_port") or self.target_port)
        if saved_pid > 0 and saved_port == self.target_port and _is_cloudflared_pid_alive(saved_pid):
            self._pid = saved_pid
            self._active_url = _normalize_https_origin(str(raw.get("active_url") or ""))
            self._started_at = int(raw.get("started_at") or time.time())
            if not self._active_url and self.mode == "quick":
                self._active_url = self._scan_log_for_quick_url()

    def _save_locked(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 1,
            "updated_at": int(time.time()),
            "target_port": self.target_port,
            "mode": self.mode,
            "auto_sync_public_url": self.auto_sync_public_url,
            "named_tunnel_token": self.named_tunnel_token,
            "named_public_url": self.named_public_url,
            "pid": self._pid,
            "active_url": self._active_url,
            "started_at": self._started_at,
        }
        try:
            atomic_write_json(self._state_path, payload, mode=0o600, dir_mode=0o700)
        except Exception as exc:
            logger.warning("保存隧道状态文件失败 (%s): %s", self._state_path, exc)

    def _scan_log_for_quick_url(self) -> str:
        if not self._log_path.is_file():
            return ""
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="ignore")
            matches = _TRYCLOUDFLARE_URL_RE.findall(text)
            return matches[-1] if matches else ""
        except Exception:
            return ""

    def _is_tunnel_registered_in_log(self) -> bool:
        if not self._log_path.is_file():
            return False
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="ignore")
            return "Registered tunnel connection" in text
        except Exception:
            return False

    def _tail_log_lines(self, max_lines: int = 12) -> list[str]:
        if not self._log_path.is_file():
            return []
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            return [ln.strip() for ln in lines if ln.strip()][-max_lines:]
        except Exception:
            return []

    def _refresh_runtime_locked(self) -> None:
        if self._pid > 0 and not _is_cloudflared_pid_alive(self._pid):
            self._pid = 0
            if self.mode == "quick":
                self._active_url = ""
            self._last_probe = None
            if not self._last_error:
                self._last_error = "公网隧道进程已退出，可点击一键重新开启。"
            self._save_locked()
        elif self._pid > 0 and self.mode == "quick" and not self._active_url:
            discovered = self._scan_log_for_quick_url()
            if discovered:
                self._active_url = discovered
                self._save_locked()
                if self.auto_sync_public_url and self._on_url_changed:
                    try:
                        self._on_url_changed(discovered)
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Lifecycle Controls
    # ------------------------------------------------------------------

    def stop(self, *, clear_public_url: bool = False) -> Dict[str, Any]:
        with self._lock:
            pid = self._pid
            self._pid = 0
            self._active_url = ""
            self._started_at = 0
            self._last_error = ""
            self._last_probe = None
            self._save_locked()

        if pid > 0 and _is_cloudflared_pid_alive(pid):
            terminate_pid_gracefully(pid, timeout_sec=2.5, expected_name_substr="cloudflared")

        if clear_public_url and self._on_url_changed:
            try:
                self._on_url_changed("")
            except Exception:
                pass
        return self.snapshot()

    async def start(
        self,
        *,
        mode: Optional[str] = None,
        named_tunnel_token: Optional[str] = None,
        named_public_url: Optional[str] = None,
        auto_sync_public_url: Optional[bool] = None,
        restart: bool = False,
        wait_seconds: float = 18.0,
    ) -> Dict[str, Any]:
        bin_path = find_cloudflared_binary()
        if not bin_path:
            install_cmd = (
                "winget install --id Cloudflare.cloudflared"
                if IS_WINDOWS
                else "brew install cloudflared"
            )
            raise RuntimeError(
                f"本机未检测到 cloudflared 工具。请先在终端执行 `{install_cmd}` 后再试。"
            )

        with self._lock:
            self._refresh_runtime_locked()
            if mode in {"quick", "named"}:
                self.mode = mode
            if named_tunnel_token is not None:
                t_clean = named_tunnel_token.strip()
                if t_clean:
                    self.named_tunnel_token = t_clean
            if named_public_url is not None:
                self.named_public_url = _normalize_https_origin(named_public_url)
            if auto_sync_public_url is not None:
                self.auto_sync_public_url = bool(auto_sync_public_url)

            if self.mode == "named" and not self.named_tunnel_token:
                raise ValueError("固定域名模式需要填写 Cloudflare Tunnel Token（如无域名请使用免登录免费模式）。")

            if self._pid > 0 and not restart:
                if self.mode == "quick" and self._active_url:
                    return self.snapshot()
                if self.mode == "named":
                    return self.snapshot()

        # Stop existing tunnel process cleanly before spawning a new one
        await asyncio.to_thread(self.stop, clear_public_url=False)

        with self._lock:
            self._log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.mode == "named":
                cmd = [
                    bin_path,
                    "tunnel",
                    "--no-autoupdate",
                    "--protocol",
                    "http2",
                    "run",
                    "--token",
                    self.named_tunnel_token,
                ]
            else:
                cmd = [
                    bin_path,
                    "tunnel",
                    "--no-autoupdate",
                    "--protocol",
                    "http2",
                    "--url",
                    f"http://127.0.0.1:{self.target_port}",
                ]

            log_fh = open(self._log_path, "w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    **subprocess_hidden_kwargs(detached=True),
                )
            finally:
                log_fh.close()
            best_effort_chmod(self._log_path, 0o600)

            self._pid = int(proc.pid)
            self._started_at = int(time.time())
            self._last_error = ""
            self._last_probe = None
            self._active_url = self.named_public_url if self.mode == "named" else ""
            self._save_locked()

        if self.mode == "quick":
            deadline = time.monotonic() + max(2.0, float(wait_seconds))
            discovered = ""
            while time.monotonic() < deadline:
                if not _is_cloudflared_pid_alive(self._pid):
                    break
                discovered = self._scan_log_for_quick_url()
                if discovered:
                    break
                await asyncio.sleep(0.25)

            if discovered and _is_cloudflared_pid_alive(self._pid):
                reg_deadline = min(deadline, time.monotonic() + 4.0)
                while time.monotonic() < reg_deadline:
                    if not _is_cloudflared_pid_alive(self._pid) or self._is_tunnel_registered_in_log():
                        break
                    await asyncio.sleep(0.25)

                disc_host = (urllib.parse.urlparse(discovered).hostname or "").strip()
                if disc_host and _is_cloudflared_pid_alive(self._pid):
                    dns_deadline = min(deadline, time.monotonic() + 5.0)
                    while time.monotonic() < dns_deadline:
                        resolved_ip = await asyncio.to_thread(_resolve_host_via_doh, disc_host, timeout=2.0)
                        if resolved_ip or not _is_cloudflared_pid_alive(self._pid):
                            break
                        await asyncio.sleep(0.6)

            with self._lock:
                if discovered:
                    self._active_url = discovered
                    self._last_error = ""
                    self._save_locked()
                    if self.auto_sync_public_url and self._on_url_changed:
                        try:
                            self._on_url_changed(discovered)
                        except Exception:
                            pass
                elif not _is_cloudflared_pid_alive(self._pid):
                    self._pid = 0
                    tail = " | ".join(self._tail_log_lines(4))
                    self._last_error = f"隧道启动失败：{tail or '请检查本机网络连接'}"
                    self._save_locked()
                    raise RuntimeError(self._last_error)
        else:
            await asyncio.sleep(1.2)
            with self._lock:
                if not _is_cloudflared_pid_alive(self._pid):
                    self._pid = 0
                    tail = " | ".join(self._tail_log_lines(4))
                    self._last_error = f"固定域名隧道启动失败：{tail or '请检查 Tunnel Token 是否正确'}"
                    self._save_locked()
                    raise RuntimeError(self._last_error)
                if self.named_public_url and self.auto_sync_public_url and self._on_url_changed:
                    try:
                        self._on_url_changed(self.named_public_url)
                    except Exception:
                        pass

        return self.snapshot()

    # ------------------------------------------------------------------
    # End-to-End Public Reachability Self-Test
    # ------------------------------------------------------------------

    def note_verified_external_traffic(self, public_origin: str) -> None:
        """Clear stale failed probe banner when genuine external traffic arrives over the active tunnel."""
        clean = _normalize_https_origin(public_origin)
        if not clean:
            return
        with self._lock:
            if clean == _normalize_https_origin(self._active_url or self.named_public_url):
                self._last_external_traffic_at = time.monotonic()
            lp = self._last_probe
            if isinstance(lp, dict) and not lp.get("ok") and _normalize_https_origin(str(lp.get("public_url") or "")) == clean:
                self._last_probe = None

    async def probe_public_reachability(
        self,
        public_url: Optional[str] = None,
        *,
        sample_api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._refresh_runtime_locked()
            target_base = _normalize_https_origin(public_url or self._active_url or self.named_public_url)
        if not target_base:
            raise ValueError("当前没有可检测的公网地址，请先开启免费公网通道或填写公网域名。")
        if not (target_base.startswith("https://") or target_base.startswith("http://")):
            target_base = "https://" + target_base

        def _sync_probe() -> Dict[str, Any]:
            ctx = ssl.create_default_context()
            ua = "GeminiEnterpriseBridge-Probe/1.0"
            pinned_ips: Dict[str, str] = {}
            t0 = time.perf_counter()

            # 1) Health check (retry briefly if trycloudflare DNS is still warming up)
            health_ok = False
            health_status = 0
            health_body: Dict[str, Any] = {}
            last_exc = ""
            failure_kind = "connection"
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    health_status, raw = _http_get_with_doh_fallback(
                        f"{target_base}/health",
                        headers={"User-Agent": ua, "Accept": "application/json"},
                        timeout=8.0,
                        ctx=ctx,
                        pinned_ip_cache=pinned_ips,
                    )
                    health_body = json.loads(raw) if raw.strip().startswith("{") else {}
                    if health_status == 200 and health_body.get("status") == "ok":
                        health_ok = True
                        break
                    last_exc = f"HTTP {health_status}"
                    failure_kind = (
                        "tunnel_disconnected"
                        if health_status == 530 and re.search(r"\b1033\b", raw)
                        else "http"
                    )
                    if health_status in {404, 502, 503, 530} and attempt < (max_attempts - 1):
                        time.sleep(1.4)
                        continue
                    break
                except Exception as exc:
                    last_exc = str(exc)
                    health_status = 0
                    failure_kind = "dns" if _is_dns_resolution_error(exc) else "connection"
                    if attempt < (max_attempts - 1):
                        time.sleep(1.4)
                        continue
            latency_ms = int((time.perf_counter() - t0) * 1000)

            if not health_ok:
                return {
                    "ok": False,
                    "tested_at": int(time.time()),
                    "public_url": target_base,
                    "latency_ms": latency_ms,
                    "health_ok": False,
                    "failure_kind": failure_kind,
                    "auth_lock_verified": False,
                    "admin_lock_verified": False,
                    "client_key_verified": None,
                    "detail": _humanize_probe_failure(last_exc, health_status),
                }

            # 2) Verify keyless public request is blocked with 401/403
            auth_lock_ok = False
            try:
                unauth_code, _ = _http_get_with_doh_fallback(
                    f"{target_base}/v1/models",
                    headers={"User-Agent": ua, "Accept": "application/json"},
                    timeout=8.0,
                    ctx=ctx,
                    pinned_ip_cache=pinned_ips,
                )
                auth_lock_ok = int(unauth_code) in {401, 403}
            except Exception:
                auth_lock_ok = False

            # 3) Verify admin console is strictly blocked from public internet (403)
            admin_lock_ok = False
            try:
                admin_code, _ = _http_get_with_doh_fallback(
                    f"{target_base}/api/status",
                    headers={"User-Agent": ua, "Accept": "application/json"},
                    timeout=8.0,
                    ctx=ctx,
                    pinned_ip_cache=pinned_ips,
                )
                admin_lock_ok = int(admin_code) == 403
            except Exception:
                admin_lock_ok = False

            # 4) Optional client key verification
            client_key_ok: Optional[bool] = None
            if sample_api_key:
                try:
                    key_code, _ = _http_get_with_doh_fallback(
                        f"{target_base}/v1/models",
                        headers={
                            "User-Agent": ua,
                            "Accept": "application/json",
                            "Authorization": f"Bearer {sample_api_key}",
                        },
                        timeout=8.0,
                        ctx=ctx,
                        pinned_ip_cache=pinned_ips,
                    )
                    client_key_ok = int(key_code) == 200
                except Exception:
                    client_key_ok = False

            all_ok = health_ok and auth_lock_ok and admin_lock_ok and (client_key_ok is not False)
            return {
                "ok": all_ok,
                "tested_at": int(time.time()),
                "public_url": target_base,
                "api_base_url": f"{target_base}/v1",
                "latency_ms": latency_ms,
                "browser_connected": bool(health_body.get("browser_connected")),
                "health_ok": health_ok,
                "auth_lock_verified": auth_lock_ok,
                "admin_lock_verified": admin_lock_ok,
                "client_key_verified": client_key_ok,
                "detail": (
                    f"公网 HTTPS 连通正常（延迟 {latency_ms}ms）· 免密访问已拦截 · 管理总台已隐藏保护"
                    if all_ok
                    else "公网已连通，但部分安全策略或客户端密钥状态异常（请检查外部接入总开关是否开启）"
                ),
            }

        result = await asyncio.to_thread(_sync_probe)
        with self._lock:
            self._last_probe = result
        return result

    async def probe_and_recover(
        self,
        public_url: Optional[str] = None,
        *,
        sample_api_key: Optional[str] = None,
        can_restart: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Retry the existing route first; replace an expired managed tunnel only when idle."""
        async with self._recovery_lock:
            with self._lock:
                original_pid = self._pid
                original_url = self._active_url
                started_at = self._started_at
                mode = self.mode
            result = await self.probe_public_reachability(public_url, sample_api_key=sample_api_key)
            target = result.get("public_url")
            if (
                result.get("health_ok") is not False
                or not original_pid
                or target != original_url
                or not started_at
                or time.time() - started_at < 60
                or (self._last_recovery_at and time.monotonic() - self._last_recovery_at < 60)
            ):
                return result

            disconnected = result.get("failure_kind") == "tunnel_disconnected"
            if not disconnected and mode == "quick" and result.get("failure_kind") == "dns":
                hostname = urllib.parse.urlparse(original_url).hostname or ""
                disconnected = await asyncio.to_thread(_quick_hostname_expired, hostname)
            if not disconnected:
                return result
            with self._lock:
                # Another admin action or genuine public traffic takes precedence over this probe.
                if (
                    self._pid != original_pid
                    or self._active_url != original_url
                    or (self._last_external_traffic_at and time.monotonic() - self._last_external_traffic_at < 60)
                ):
                    return result
            if can_restart is not None and not can_restart():
                result["detail"] += "；当前有任务正在执行或排队，待任务结束后再次自检即可修复"
                return result

            self._last_recovery_at = time.monotonic()
            try:
                snap = await self.start(restart=True)
                new_url = snap.get("active_url")
                if not new_url:
                    raise RuntimeError("尚未取得可用的公网地址，请稍后再次自检")
                result = await self.probe_public_reachability(new_url, sample_api_key=sample_api_key)
                result["recovery_attempted"] = True
                result["url_changed"] = new_url != original_url
                if result["url_changed"]:
                    result["detail"] = "已重新申请公网通道；请将外部软件更新为页面上的新地址。" + result["detail"]
                else:
                    result["detail"] = (
                        "已重新连接原公网通道。" if result.get("health_ok") else "已尝试重新连接原公网通道，仍未连通。"
                    ) + result["detail"]
            except (RuntimeError, ValueError, OSError) as exc:
                result["recovery_attempted"] = True
                result["detail"] = f"公网连接自动修复未完成：{exc}；请检查网络后再次自检"
            with self._lock:
                self._last_probe = result
            return result

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_runtime_locked()
            cf_bin = find_cloudflared_binary()
            ts_bin = find_tailscale_binary()
            running = self._pid > 0 and _is_cloudflared_pid_alive(self._pid)
            eff_url = self._active_url or (self.named_public_url if running and self.mode == "named" else "")
            return {
                "cloudflared_installed": bool(cf_bin),
                "tailscale_installed": bool(ts_bin),
                "running": running,
                "pid": self._pid if running else 0,
                "mode": self.mode,
                "target_port": self.target_port,
                "active_url": eff_url,
                "api_base_url": f"{eff_url}/v1" if eff_url else "",
                "auto_sync_public_url": self.auto_sync_public_url,
                "has_named_token": bool(self.named_tunnel_token),
                "named_public_url": self.named_public_url,
                "started_at": self._started_at if running else 0,
                "last_error": self._last_error,
                "last_probe": self._last_probe,
                "recent_logs": self._tail_log_lines(6),
            }

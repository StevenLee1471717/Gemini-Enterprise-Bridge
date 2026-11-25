"""Cross-platform OS primitives for Windows 10/11, macOS, and Linux."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

IS_WINDOWS = os.name == "nt"

_UTC_PLUS_8 = timezone(timedelta(hours=8), name="Asia/Shanghai")
BILLING_TIMEZONE = "America/Los_Angeles"


class _USPacificFallbackTz(tzinfo):
    """Stdlib US Pacific (PST/PDT) fallback when IANA tzdata is absent on bare Windows."""

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> int:
        first_weekday = datetime(year, month, 1).weekday()  # Mon=0 .. Sun=6
        first_sunday = 1 + ((6 - first_weekday) % 7)
        return first_sunday + (n - 1) * 7

    def _is_dst(self, dt: Optional[datetime]) -> bool:
        if dt is None:
            return False
        year = dt.year
        dst_start = datetime(year, 3, self._nth_sunday(year, 3, 2), 2, 0)
        dst_end = datetime(year, 11, self._nth_sunday(year, 11, 1), 2, 0)
        naive = dt.replace(tzinfo=None)
        return dst_start <= naive < dst_end

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=-7 if self._is_dst(dt) else -8)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "PDT" if self._is_dst(dt) else "PST"


_PACIFIC_FALLBACK_TZ = _USPacificFallbackTz()


def resolve_timezone(name: str = "Asia/Shanghai") -> tzinfo:
    """Return IANA ZoneInfo when available, falling back cleanly on bare Windows Python."""
    try:
        return ZoneInfo(name)
    except Exception:
        if name in {"America/Los_Angeles", "US/Pacific", "PST8PDT"}:
            return _PACIFIC_FALLBACK_TZ
        if name in {"Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Singapore", "PRC"}:
            return _UTC_PLUS_8
        if name.upper() == "UTC":
            return timezone.utc
        return datetime.now().astimezone().tzinfo or _UTC_PLUS_8


try:
    SHANGHAI_TZ: tzinfo = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI_TZ = _UTC_PLUS_8

try:
    PACIFIC_TZ: tzinfo = ZoneInfo(BILLING_TIMEZONE)
except Exception:
    PACIFIC_TZ = _PACIFIC_FALLBACK_TZ


def billing_day_key(ts: Optional[float] = None, billing_tz_name: str = BILLING_TIMEZONE) -> str:
    """Return YYYY-MM-DD billing day anchored to 00:00 California (16:00 PST / 15:00 PDT in Shanghai)."""
    eff_ts = float(time.time() if ts is None else ts)
    tz_obj = PACIFIC_TZ if billing_tz_name == BILLING_TIMEZONE else resolve_timezone(billing_tz_name)
    return datetime.fromtimestamp(eff_ts, tz=tz_obj).strftime("%Y-%m-%d")


def billing_window_info(
    now_ts: Optional[float] = None,
    billing_tz_name: str = BILLING_TIMEZONE,
    local_tz_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Compute current billing day boundaries and local reset time (16:00 PST / 15:00 PDT)."""
    eff_ts = float(time.time() if now_ts is None else now_ts)
    b_tz = PACIFIC_TZ if billing_tz_name == BILLING_TIMEZONE else resolve_timezone(billing_tz_name)
    l_tz = SHANGHAI_TZ if local_tz_name == "Asia/Shanghai" else resolve_timezone(local_tz_name)

    now_b = datetime.fromtimestamp(eff_ts, tz=b_tz)
    cur_date = now_b.date()
    prev_date = cur_date - timedelta(days=1)
    next_date = cur_date + timedelta(days=1)

    start_b = datetime(cur_date.year, cur_date.month, cur_date.day, 0, 0, 0, tzinfo=b_tz)
    end_b = datetime(next_date.year, next_date.month, next_date.day, 0, 0, 0, tzinfo=b_tz)
    start_ts = int(start_b.timestamp())
    end_ts = int(end_b.timestamp())

    start_local = datetime.fromtimestamp(start_ts, tz=l_tz)
    end_local = datetime.fromtimestamp(end_ts, tz=l_tz)
    span = max(1, end_ts - start_ts)
    elapsed_pct = round(min(100.0, max(0.0, ((eff_ts - start_ts) / span) * 100.0)), 1)

    return {
        "billing_day": cur_date.isoformat(),
        "yesterday_billing_day": prev_date.isoformat(),
        "billing_timezone": billing_tz_name,
        "display_timezone": local_tz_name,
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "window_start_local": start_local.strftime("%m-%d %H:%M"),
        "window_end_local": end_local.strftime("%m-%d %H:%M"),
        "window_label": f"{start_local.strftime('%m-%d %H:%M')} → {end_local.strftime('%m-%d %H:%M')}",
        "reset_time_local": start_local.strftime("%H:%M"),
        "seconds_until_reset": max(0, int(round(end_ts - eff_ts))),
        "elapsed_pct": elapsed_pct,
    }


def reconfigure_stdio_utf8(*, force_utf8: bool = False, errors: str = "replace") -> None:
    """Prevent UnicodeEncodeError/UnicodeDecodeError on GBK/cp936 Windows consoles & pipes."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconf = getattr(stream, "reconfigure", None)
        if not callable(reconf):
            continue
        try:
            if force_utf8:
                kwargs: dict[str, Any] = {"encoding": "utf-8", "errors": errors}
                if stream_name == "stdout":
                    kwargs["newline"] = "\n"
                reconf(**kwargs)
            elif IS_WINDOWS:
                enc = (getattr(stream, "encoding", None) or "").lower()
                if enc not in {"utf-8", "utf8"}:
                    reconf(encoding="utf-8", errors=errors)
                else:
                    reconf(errors=errors)
        except Exception:
            pass


def subprocess_hidden_kwargs(*, detached: bool = False) -> dict[str, Any]:
    """Return subprocess kwargs that suppress console window popups on Windows."""
    if IS_WINDOWS:
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        if detached:
            flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        return {"creationflags": flags}
    if detached:
        return {"start_new_session": True, "close_fds": True}
    return {}


def best_effort_chmod(path: Path | str, mode: int = 0o600) -> None:
    """Apply POSIX file mode while ignoring unsupported NTFS permission semantics."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_text(
    path: Path | str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
    dir_mode: int = 0o700,
    retries: int = 5,
) -> None:
    """Atomically write UTF-8 text with Windows Defender / Search Indexer lock retries."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    closed = False
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            closed = True
            fh.write(content)
            fh.flush()
        best_effort_chmod(tmp_name, mode)
        last_err: Optional[OSError] = None
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                os.replace(tmp_name, target)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.015 * (2 ** attempt))
        if last_err is not None:
            raise last_err
    finally:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_json(
    path: Path | str,
    payload: Any,
    *,
    mode: int = 0o600,
    dir_mode: int = 0o700,
    retries: int = 5,
) -> None:
    """Atomically serialize JSON to disk with UTF-8 encoding and Windows lock retries."""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text, encoding="utf-8", mode=mode, dir_mode=dir_mode, retries=retries)


def sqlite_file_uri(db_path: Path | str, mode: str = "ro") -> str:
    """Build an RFC 3986 SQLite URI supporting Windows drive letters, UNC shares, spaces, and CJK."""
    raw = str(db_path)
    # Handle explicit Windows paths even when unit-tested on POSIX
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        norm = raw.replace("\\", "/")
        drive = norm[:2]
        rest = quote(norm[2:], safe="/")
        return f"file:///{drive}{rest}?mode={mode}"
    if raw.startswith("\\\\") or raw.startswith("//"):
        norm = raw.lstrip("\\/").replace("\\", "/")
        encoded = quote(norm, safe="/")
        return f"file://{encoded}?mode={mode}"
    resolved = Path(db_path).resolve()
    base_uri = resolved.as_uri()
    sep = "&" if "?" in base_uri else "?"
    return f"{base_uri}{sep}mode={mode}"


def configure_sqlite_connection(conn: sqlite3.Connection, *, is_memory: bool = False) -> None:
    """Enable busy_timeout and WAL mode to prevent SQLite lock contention on NTFS."""
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        pass


def _expand_windows_candidates(patterns: Sequence[str]) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        expanded = os.path.expandvars(os.path.expanduser(pat))
        if "*" in expanded:
            parent = Path(expanded).parent
            name_pat = Path(expanded).name
            try:
                if parent.exists():
                    for match in sorted(parent.glob(name_pat), reverse=True):
                        out.append(str(match))
            except OSError:
                pass
        else:
            out.append(expanded)
    # Also search WinGet Packages directory when Links shim is not yet in PATH
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data and patterns:
        exe_names = {Path(p).name.lower() for p in patterns if Path(p).suffix.lower() in {".exe", ".cmd"}}
        pkgs_dir = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        try:
            if exe_names and pkgs_dir.is_dir():
                for pkg in pkgs_dir.iterdir():
                    if not pkg.is_dir():
                        continue
                    low_pkg = pkg.name.lower()
                    if not any(stem.split(".", 1)[0] in low_pkg for stem in exe_names):
                        continue
                    for found in pkg.rglob("*"):
                        if found.name.lower() in exe_names and found.is_file():
                            out.append(str(found))
        except OSError:
            pass
    return out


def find_executable(
    name: str,
    *,
    posix_candidates: Sequence[str] = (),
    windows_candidates: Sequence[str] = (),
) -> Optional[str]:
    """Locate an executable across PATH and platform-standard installation directories."""
    probe_names = [name]
    if IS_WINDOWS and "." not in Path(name).name:
        probe_names.extend([f"{name}.exe", f"{name}.cmd", f"{name}.bat"])
    for candidate_name in probe_names:
        found = shutil.which(candidate_name)
        if found and os.path.isfile(found):
            return found

    extra = _expand_windows_candidates(windows_candidates) if IS_WINDOWS else list(posix_candidates)
    for candidate in extra:
        try:
            if os.path.isfile(candidate) and (IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
        except OSError:
            continue
    return None


def _windows_query_pid(pid: int) -> tuple[bool, str]:
    """Safely inspect a Windows PID without sending CTRL_C_EVENT via os.kill(pid, 0)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED (5) means the PID exists under another security context
            if err == 5:
                return True, ""
            return False, ""
        try:
            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != STILL_ACTIVE:
                    return False, ""

            buf_len = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            img_name = ""
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                img_name = buf.value or ""
            return True, img_name
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.5,
            check=False,
            **subprocess_hidden_kwargs(),
        )
        for row in csv.reader(io.StringIO(out.stdout or "")):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True, row[0].strip()
    except Exception:
        pass
    return False, ""


def is_pid_alive(pid: int, expected_name_substr: str = "") -> bool:
    """Return True if `pid` is currently running (and matches `expected_name_substr` when inspectable)."""
    if pid <= 0:
        return False
    needle = (expected_name_substr or "").strip().lower()
    if IS_WINDOWS:
        alive, img = _windows_query_pid(pid)
        if not alive:
            return False
        if needle and img:
            return needle in img.lower()
        return True

    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    if not needle:
        return True
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=1.5,
            check=False,
        )
        cmd_line = (out.stdout or "").strip().lower()
        if cmd_line:
            return needle in cmd_line
    except Exception:
        pass
    return True


def terminate_pid_gracefully(
    pid: int,
    *,
    timeout_sec: float = 2.5,
    expected_name_substr: str = "",
) -> None:
    """Stop a background child process cleanly on both Windows and POSIX."""
    if pid <= 0 or not is_pid_alive(pid, expected_name_substr):
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=2.0,
                check=False,
                **subprocess_hidden_kwargs(),
            )
        except Exception:
            pass
        deadline = time.monotonic() + max(0.3, timeout_sec)
        while time.monotonic() < deadline and is_pid_alive(pid, expected_name_substr):
            time.sleep(0.08)
        if is_pid_alive(pid, expected_name_substr):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=3.0,
                    check=False,
                    **subprocess_hidden_kwargs(),
                )
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + max(0.3, timeout_sec)
    while time.monotonic() < deadline and is_pid_alive(pid, expected_name_substr):
        time.sleep(0.08)
    if is_pid_alive(pid, expected_name_substr):
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass

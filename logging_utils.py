"""Structured logging, UTF-8 console/rotating-file handlers, and runtime debug level control."""

from __future__ import annotations

import collections
import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import time
from typing import Any, Deque, Dict, List, Optional

TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

_LEVEL_NAME_TO_NUM: Dict[str, int] = {
    "TRACE": TRACE_LEVEL_NUM,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("bridge_req_id", default="-")


def normalize_log_level(raw_level: Optional[str], debug_flag: bool = False) -> str:
    if debug_flag and (not raw_level or raw_level.strip().upper() == "INFO"):
        return "DEBUG"
    cleaned = (raw_level or "INFO").strip().upper()
    if cleaned == "WARN":
        return "WARNING"
    return cleaned if cleaned in _LEVEL_NAME_TO_NUM else "INFO"


def level_to_int(level_name: str) -> int:
    return _LEVEL_NAME_TO_NUM.get(normalize_log_level(level_name), logging.INFO)


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "req_id"):
            record.req_id = request_id_var.get("-")
        return True


class _RingBufferHandler(logging.Handler):
    """Thread-safe in-memory ring buffer of recent backend log records."""

    def __init__(self, maxlen: int = 400) -> None:
        super().__init__(level=TRACE_LEVEL_NUM)
        self._records: Deque[Dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                msg = f"{msg}\n{formatter.formatException(record.exc_info)}"
            entry = {
                "ts": round(record.created, 3),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "req_id": getattr(record, "req_id", request_id_var.get("-")),
                "message": msg[:4000],
            }
            with self._lock:
                self._records.append(entry)
        except Exception:
            pass

    def snapshot(
        self,
        limit: int = 120,
        min_level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        min_num = level_to_int(min_level) if min_level else TRACE_LEVEL_NUM
        needle = (search or "").strip().lower()
        with self._lock:
            items = list(self._records)
        filtered: List[Dict[str, Any]] = []
        for item in items:
            if _LEVEL_NAME_TO_NUM.get(item["level"], logging.INFO) < min_num:
                continue
            if needle and needle not in item["message"].lower() and needle not in item["logger"].lower() and needle not in str(item.get("req_id") or "").lower():
                continue
            filtered.append(item)
        return filtered[-max(1, min(limit, 400)) :]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class BridgeLogManager:
    """Central manager for Gemini Enterprise Bridge console, file, and ring-buffer logging."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ring_handler = _RingBufferHandler(maxlen=400)
        self.ctx_filter = _RequestContextFilter()
        self.console_handler: Optional[logging.Handler] = None
        self.file_handler: Optional[RotatingFileHandler] = None
        self.current_level: str = "INFO"
        self.log_file_path: str = ""
        self.log_payloads: bool = False
        self._configured = False

    def configure(
        self,
        level: str = "INFO",
        debug: bool = False,
        log_file: Optional[str] = None,
        log_payloads: bool = False,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> Dict[str, Any]:
        with self._lock:
            resolved_level = normalize_log_level(level, debug_flag=debug)
            level_num = level_to_int(resolved_level)
            self.current_level = resolved_level
            self.log_payloads = bool(log_payloads or resolved_level == "TRACE")

            for stream_name in ("stdout", "stderr"):
                stream_obj = getattr(sys, stream_name, None)
                reconf = getattr(stream_obj, "reconfigure", None)
                if callable(reconf):
                    try:
                        reconf(encoding="utf-8", errors="replace")
                    except Exception:
                        pass

            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] [req=%(req_id)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            self.ring_handler.setFormatter(fmt)
            self.ring_handler.addFilter(self.ctx_filter)

            root_logger = logging.getLogger()
            if root_logger.level > level_num or not self._configured:
                root_logger.setLevel(min(root_logger.level or logging.INFO, level_num))

            bridge_root = logging.getLogger("gemini_bridge")
            bridge_root.setLevel(level_num)
            bridge_root.propagate = False

            if self.console_handler is None:
                self.console_handler = logging.StreamHandler(sys.stderr)
                self.console_handler.setFormatter(fmt)
                self.console_handler.addFilter(self.ctx_filter)
            self.console_handler.setLevel(level_num)

            if self.console_handler not in bridge_root.handlers:
                bridge_root.addHandler(self.console_handler)
            if self.ring_handler not in bridge_root.handlers:
                bridge_root.addHandler(self.ring_handler)

            target_file = (log_file or "").strip()
            if target_file != self.log_file_path:
                if self.file_handler is not None:
                    bridge_root.removeHandler(self.file_handler)
                    try:
                        self.file_handler.close()
                    except Exception:
                        pass
                    self.file_handler = None
                self.log_file_path = ""
                if target_file:
                    try:
                        fp = Path(target_file)
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fh = RotatingFileHandler(
                            fp,
                            maxBytes=max(256 * 1024, max_bytes),
                            backupCount=max(1, backup_count),
                            encoding="utf-8",
                        )
                        fh.setFormatter(fmt)
                        fh.addFilter(self.ctx_filter)
                        fh.setLevel(level_num)
                        bridge_root.addHandler(fh)
                        self.file_handler = fh
                        self.log_file_path = str(fp.resolve())
                    except Exception as exc:
                        bridge_root.warning("无法初始化日志文件 %s: %s", target_file, exc)
            elif self.file_handler is not None:
                self.file_handler.setLevel(level_num)

            for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                uv_logger = logging.getLogger(uv_name)
                uv_logger.setLevel(logging.DEBUG if level_num <= logging.DEBUG else logging.INFO)

            self._configured = True
            return self.status()

    def set_level(self, level: str, log_payloads: Optional[bool] = None) -> Dict[str, Any]:
        return self.configure(
            level=level,
            debug=(normalize_log_level(level) in {"DEBUG", "TRACE"}),
            log_file=self.log_file_path or None,
            log_payloads=self.log_payloads if log_payloads is None else bool(log_payloads),
        )

    def is_debug_enabled(self) -> bool:
        return level_to_int(self.current_level) <= logging.DEBUG

    def status(self) -> Dict[str, Any]:
        return {
            "level": self.current_level,
            "numeric_level": level_to_int(self.current_level),
            "debug_enabled": self.is_debug_enabled(),
            "log_payloads": self.log_payloads,
            "log_file": self.log_file_path,
            "buffered_entries": len(self.ring_handler._records),
        }

    def get_logs(
        self,
        limit: int = 120,
        min_level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.ring_handler.snapshot(limit=limit, min_level=min_level, search=search)


LOG_MANAGER = BridgeLogManager()


def summarize_for_debug(obj: Any, max_chars: int = 600) -> str:
    """Serialize payloads safely for DEBUG/TRACE logs while redacting inline base64/secrets."""
    def _scrub(val: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "<...>"
        if isinstance(val, str):
            if val.startswith("data:") and ";base64," in val:
                head = val.split(",", 1)[0]
                return f"{head},<b64 len={len(val)}>"
            if len(val) > 320:
                return f"{val[:240]}... (len={len(val)})"
            return val
        if isinstance(val, (bytes, bytearray)):
            return f"<bytes len={len(val)}>"
        if isinstance(val, dict):
            out: Dict[str, Any] = {}
            for k, v in list(val.items())[:30]:
                kl = str(k).lower()
                if any(s in kl for s in ("api_key", "authorization", "secret", "token", "password")) and isinstance(v, str) and len(v) > 4:
                    out[str(k)] = f"{v[:4]}***"
                elif kl in {"file_data", "data_b64", "b64_json"} and isinstance(v, str) and len(v) > 80:
                    out[str(k)] = f"<b64 len={len(v)}>"
                else:
                    out[str(k)] = _scrub(v, depth + 1)
            return out
        if isinstance(val, list):
            if len(val) > 12:
                return [_scrub(x, depth + 1) for x in val[:12]] + [f"... (+{len(val) - 12} items)"]
            return [_scrub(x, depth + 1) for x in val]
        return val

    try:
        rendered = json.dumps(_scrub(obj), ensure_ascii=False)
    except Exception:
        rendered = repr(obj)
    if len(rendered) > max_chars:
        return f"{rendered[:max_chars]}...(truncated)"
    return rendered

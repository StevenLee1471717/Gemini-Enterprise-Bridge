"""Public-facing availability monitor, strict probe endpoint helpers, and status dashboard HTML.

Designed for public distribution over Cloudflare Quick/Named Tunnels or reverse proxies:
- Zero Google quota consumption: heartbeats inspect live worker readiness every 60s without
  sending synthetic prompts to Gemini Enterprise.
- Passive real dialogue latency: aggregates recent completed request durations from UsageLedger
  while stripping all tenant, IP, prompt, session, and billing details.
- Real hardware multi-region HTTP probing: integrates Globalping verified probes across Mainland
  China (Telecom / Unicom / Mobile) and HK/TW/JP/SG/US to measure true end-to-end tunnel RTT
  (not deceptive Anycast TCP edge handshakes), plus ITDOG / Chinaz / Boce cross-check links.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from platform_compat import SHANGHAI_TZ, atomic_write_json
from usage_ledger import resolve_canonical_billing_model

HISTORY_BAR_COUNT = 60
MAX_STORED_TICKS = 120
MAX_STORED_DAYS = 14
SAMPLE_INTERVAL_SEC = 60
CLIENT_REFRESH_SEC = 45
NETWORK_MAP_CACHE_TTL_SEC = 90
GLOBALPING_API_BASE = "https://api.globalping.io/v1/measurements"

CITY_ZH_MAP: Dict[str, str] = {
    "beijing": "北京",
    "shanghai": "上海",
    "guangzhou": "广东·广州",
    "shenzhen": "广东·深圳",
    "dongguan": "广东·东莞",
    "foshan": "广东·佛山",
    "taishan": "广东·台山",
    "chengdu": "四川·成都",
    "chongqing": "重庆",
    "wuhan": "湖北·武汉",
    "shiyan": "湖北·十堰",
    "xi'an": "陕西·西安",
    "xian": "陕西·西安",
    "changsha": "湖南·长沙",
    "zhengzhou": "河南·郑州",
    "nanjing": "江苏·南京",
    "suzhou": "江苏·苏州",
    "wuxi": "江苏·无锡",
    "yancheng": "江苏·盐城",
    "hangzhou": "浙江·杭州",
    "ningbo": "浙江·宁波",
    "jinhua": "浙江·金华",
    "wenzhou": "浙江·温州",
    "jinan": "山东·济南",
    "qingdao": "山东·青岛",
    "wuhu": "安徽·芜湖",
    "hefei": "安徽·合肥",
    "fuzhou": "福建·福州",
    "xiamen": "福建·厦门",
    "nanning": "广西·南宁",
    "guilin": "广西·桂林",
    "tianjin": "天津",
    "shenyang": "辽宁·沈阳",
    "dalian": "辽宁·大连",
    "harbin": "黑龙江·哈尔滨",
    "kunming": "云南·昆明",
    "guiyang": "贵州·贵阳",
    "hong kong": "中国香港",
    "taipei": "中国台湾·台北",
    "taichung": "中国台湾·台中",
    "kaohsiung": "中国台湾·高雄",
    "tokyo": "日本·东京",
    "osaka": "日本·大阪",
    "singapore": "新加坡",
    "seoul": "韩国·首尔",
    "los angeles": "美国·洛杉矶",
    "san jose": "美国·圣何塞",
    "seattle": "美国·西雅图",
    "new york": "美国·纽约",
    "buffalo": "美国·纽约州",
    "dallas": "美国·达拉斯",
    "frankfurt": "德国·法兰克福",
}

COUNTRY_ZH_MAP: Dict[str, str] = {
    "CN": "中国内地",
    "HK": "中国香港",
    "MO": "中国澳门",
    "TW": "中国台湾",
    "JP": "日本",
    "SG": "新加坡",
    "KR": "韩国",
    "US": "美国",
    "DE": "德国",
    "GB": "英国",
}

MONITORED_MODELS: tuple[Dict[str, Any], ...] = (
    {
        "id": "gemini-4-carbon",
        "canonical": "gemini-4-carbon",
        "title": "Gemini-4-Carbon",
        "provider": "OpenAI 兼容",
        "pool": "默认主力旗舰",
        "is_primary": True,
    },
    {
        "id": "gemini-4-argon",
        "canonical": "gemini-4-argon",
        "title": "Gemini-4-Argon",
        "provider": "OpenAI 兼容",
        "pool": "高阶推理号池",
        "is_primary": False,
    },
    {
        "id": "gemini-3.1-pro",
        "canonical": "gemini-3.1-pro",
        "title": "Gemini-3.1-Pro",
        "provider": "OpenAI 兼容",
        "pool": "通用长文号池",
        "is_primary": False,
    },
    {
        "id": "gemini-3.8-flash",
        "canonical": "gemini-3.8-flash",
        "title": "Gemini-3.8-Flash",
        "provider": "OpenAI 兼容",
        "pool": "轻量高速号池",
        "is_primary": False,
    },
)


def _day_key_shanghai(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=SHANGHAI_TZ)
    return dt.strftime("%Y-%m-%d")


def _is_local_origin(origin: str) -> bool:
    low = (origin or "").strip().lower()
    if not low:
        return True
    for marker in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0", "testserver"):
        if marker in low:
            return True
    return False


def build_external_speedtest_urls(probe_url: str) -> Dict[str, str]:
    """Build verified third-party multi-ISP speedtest URLs (ITDOG, Chinaz, Boce)."""
    clean = (probe_url or "").strip()
    parsed = urllib.parse.urlparse(clean if "://" in clean else f"https://{clean}")
    host = parsed.netloc or ""
    if not clean or _is_local_origin(clean) or not host:
        return {
            "itdog_url": "https://www.itdog.cn/http/",
            "chinaz_url": "https://tool.chinaz.com/speedtest",
            "boce_url": "https://www.boce.com/http",
        }
    return {
        "itdog_url": "https://www.itdog.cn/http/",
        "chinaz_url": f"https://tool.chinaz.com/speedtest/{clean}",
        "boce_url": f"https://www.boce.com/http/{host}",
    }


def _classify_isp(country: str, asn: int, network_name: str) -> tuple[str, str]:
    net_low = (network_name or "").lower()
    if country != "CN":
        if country in {"HK", "MO", "TW"}:
            return "hkmotw", COUNTRY_ZH_MAP.get(country, "港澳台")
        return "overseas", "海外网络"
    if asn in {4134, 4809, 4812, 23724} or "telecom" in net_low or "chinanet" in net_low:
        return "telecom", "中国电信"
    if asn in {4837, 9929, 17621, 17623} or "unicom" in net_low or "china169" in net_low:
        return "unicom", "中国联通"
    if asn in {9808, 56040, 56041, 56046, 56047, 56048, 24400} or "mobile" in net_low or "cmnet" in net_low:
        return "mobile", "中国移动"
    if any(k in net_low for k in ("alibaba", "aliyun", "tencent", "huawei", "volcengine", "baidu")):
        return "cloud", "国内云BGP"
    return "cn_other", "国内宽带"


def _localize_probe_region(country: str, city: str) -> str:
    city_clean = (city or "").strip()
    mapped = CITY_ZH_MAP.get(city_clean.lower())
    if mapped:
        return mapped
    c_zh = COUNTRY_ZH_MAP.get(country, country or "未知地区")
    if city_clean:
        return f"{c_zh}·{city_clean}"
    return c_zh


class PublicStatusMonitor:
    """Persists lightweight 60-second availability heartbeats and builds sanitized public snapshots."""

    def __init__(
        self,
        storage_path: Optional[str | Path] = None,
        sample_interval_sec: int = SAMPLE_INTERVAL_SEC,
    ) -> None:
        self._lock = threading.Lock()
        self.storage_path: Optional[Path] = Path(storage_path) if storage_path else None
        self.sample_interval_sec = max(10, int(sample_interval_sec or SAMPLE_INTERVAL_SEC))
        self._started_at = int(time.time())
        self._last_sample_ts: int = 0
        self._recent_ticks: List[Dict[str, Any]] = []
        self._daily_rollup: Dict[str, Dict[str, int]] = {}
        self._network_map_cache: Optional[Dict[str, Any]] = None
        self._network_map_async_lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            import json

            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            ticks = raw.get("recent_ticks")
            if isinstance(ticks, list):
                cleaned_ticks: List[Dict[str, Any]] = []
                for item in ticks[-MAX_STORED_TICKS:]:
                    if isinstance(item, dict) and isinstance(item.get("ts"), (int, float)):
                        st = str(item.get("state") or "up")
                        if st not in {"up", "busy", "down"}:
                            st = "up"
                        cleaned_ticks.append(
                            {
                                "ts": int(item["ts"]),
                                "state": st,
                                "chat_ms": int(item["chat_ms"]) if item.get("chat_ms") else None,
                            }
                        )
                self._recent_ticks = cleaned_ticks
                if cleaned_ticks:
                    self._last_sample_ts = int(cleaned_ticks[-1]["ts"])
            days = raw.get("daily_rollup")
            if isinstance(days, dict):
                cleaned_days: Dict[str, Dict[str, int]] = {}
                for k, v in days.items():
                    if isinstance(k, str) and isinstance(v, dict):
                        cleaned_days[k] = {
                            "up": max(0, int(v.get("up") or 0)),
                            "busy": max(0, int(v.get("busy") or 0)),
                            "down": max(0, int(v.get("down") or 0)),
                        }
                self._daily_rollup = cleaned_days
        except Exception:
            return

    def _save_locked(self) -> None:
        if not self.storage_path:
            return
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": int(time.time()),
                "recent_ticks": self._recent_ticks[-MAX_STORED_TICKS:],
                "daily_rollup": self._daily_rollup,
            }
            atomic_write_json(self.storage_path, payload, mode=0o600)
        except Exception:
            pass

    def _prune_old_days_locked(self, now_ts: float) -> None:
        cutoff = (datetime.fromtimestamp(now_ts, tz=SHANGHAI_TZ) - timedelta(days=MAX_STORED_DAYS)).strftime(
            "%Y-%m-%d"
        )
        stale = [d for d in self._daily_rollup if d < cutoff]
        for d in stale:
            self._daily_rollup.pop(d, None)

    def record_sample(
        self,
        *,
        online: bool,
        busy: bool = False,
        latest_chat_ms: Optional[int] = None,
        now_ts: Optional[float] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        state = "down" if not online else ("busy" if busy else "up")
        with self._lock:
            if not force and self._last_sample_ts > 0 and (ts - self._last_sample_ts) < (self.sample_interval_sec - 5):
                return self._recent_ticks[-1] if self._recent_ticks else {"ts": ts, "state": state, "chat_ms": latest_chat_ms}

            tick = {
                "ts": ts,
                "state": state,
                "chat_ms": int(latest_chat_ms) if (latest_chat_ms and latest_chat_ms > 0) else None,
            }
            self._recent_ticks.append(tick)
            if len(self._recent_ticks) > MAX_STORED_TICKS:
                self._recent_ticks = self._recent_ticks[-MAX_STORED_TICKS:]
            self._last_sample_ts = ts

            day_key = _day_key_shanghai(ts)
            bucket = self._daily_rollup.setdefault(day_key, {"up": 0, "busy": 0, "down": 0})
            bucket[state] = int(bucket.get(state, 0)) + 1
            self._prune_old_days_locked(ts)
            self._save_locked()
            return tick

    def _compute_7d_uptime_locked(self, now_ts: float) -> float:
        anchor = datetime.fromtimestamp(now_ts, tz=SHANGHAI_TZ).date()
        start_key = (anchor - timedelta(days=6)).isoformat()
        end_key = anchor.isoformat()
        up_cnt = 0
        total_cnt = 0
        for dk, counts in self._daily_rollup.items():
            if start_key <= dk <= end_key:
                u = int(counts.get("up") or 0) + int(counts.get("busy") or 0)
                d = int(counts.get("down") or 0)
                up_cnt += u
                total_cnt += u + d
        if total_cnt <= 0:
            return 100.0
        return round((up_cnt / total_cnt) * 100.0, 2)

    @staticmethod
    def _extract_sanitized_ledger_metrics(usage_ledger: Any, now_ts: float) -> Dict[str, Any]:
        """Extract strictly non-sensitive latency and success metrics from UsageLedger."""
        if usage_ledger is None:
            return {"gateway_chat_ms": None, "last_chat_ts": None, "by_model": {}, "gateway_call_ticks": []}

        lock = getattr(usage_ledger, "_lock", None)
        raw_list: Sequence[Dict[str, Any]] = ()
        if lock is not None:
            with lock:
                raw_list = list(getattr(usage_ledger, "_recent_requests", ()) or ())
        else:
            raw_list = list(getattr(usage_ledger, "_recent_requests", ()) or ())

        cutoff_7d = now_ts - 7 * 86400
        chat_durations: List[int] = []
        last_chat_ts: Optional[int] = None
        gateway_call_ticks: List[Dict[str, Any]] = []
        by_model_calls: Dict[str, List[Dict[str, Any]]] = {}

        for rec in raw_list:
            if not isinstance(rec, dict):
                continue
            status_code = int(rec.get("status_code") or 200)
            # Ignore client auth / quota / bad-request rejections — they do not reflect upstream health.
            if status_code in {400, 401, 403, 404, 422, 429}:
                continue
            ts = int(rec.get("ts") or 0)
            ok = str(rec.get("status") or "ok") == "ok" and status_code < 500
            dur = max(0, int(rec.get("duration_ms") or 0))
            task_type = str(rec.get("task_type") or "chat")
            canonical = resolve_canonical_billing_model(
                requested_model=str(rec.get("requested_model") or rec.get("model") or "")
            )

            if ok and dur > 0 and task_type in {"chat", "responses"}:
                chat_durations.append(dur)
                if last_chat_ts is None or ts > last_chat_ts:
                    last_chat_ts = ts

            tick_item = {
                "ts": ts,
                "state": "up" if ok else "down",
                "chat_ms": dur if (ok and dur > 0) else None,
            }
            gateway_call_ticks.append(tick_item)
            if ts >= cutoff_7d:
                by_model_calls.setdefault(canonical, []).append(tick_item)

        gateway_chat_ms: Optional[int] = None
        if chat_durations:
            recent_window = chat_durations[-5:]
            gateway_chat_ms = int(round(statistics.median(recent_window)))

        model_summaries: Dict[str, Dict[str, Any]] = {}
        for m_key, items in by_model_calls.items():
            ok_durs = [int(x["chat_ms"]) for x in items if x["state"] == "up" and x.get("chat_ms")]
            up_n = sum(1 for x in items if x["state"] == "up")
            tot_n = len(items)
            model_summaries[m_key] = {
                "chat_latency_ms": int(round(statistics.median(ok_durs[-5:]))) if ok_durs else None,
                "calls_7d": tot_n,
                "success_rate_7d": round((up_n / tot_n) * 100.0, 2) if tot_n > 0 else None,
                "last_state": items[-1]["state"] if items else "up",
                "call_ticks": items[-HISTORY_BAR_COUNT:],
            }

        return {
            "gateway_chat_ms": gateway_chat_ms,
            "last_chat_ts": last_chat_ts,
            "by_model": model_summaries,
            "gateway_call_ticks": gateway_call_ticks[-HISTORY_BAR_COUNT:],
        }

    @staticmethod
    def _pad_ticks(ticks: Sequence[Dict[str, Any]], count: int = HISTORY_BAR_COUNT) -> List[Dict[str, Any]]:
        sliced = list(ticks[-count:])
        if len(sliced) < count:
            pad = [{"ts": 0, "state": "empty", "chat_ms": None} for _ in range(count - len(sliced))]
            return pad + sliced
        return sliced

    def build_public_snapshot(
        self,
        *,
        online: bool,
        busy: bool,
        public_origin: str,
        usage_ledger: Any = None,
        server_probe_ms: Optional[int] = None,
        local_only_view: bool = False,
        tunnel_probe_failed: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        ts = float(now_ts if now_ts is not None else time.time())
        ledger_stats = self._extract_sanitized_ledger_metrics(usage_ledger, ts)
        gateway_chat_ms = ledger_stats["gateway_chat_ms"]
        reachable = online and not tunnel_probe_failed

        # Ensure at least one fresh heartbeat exists even if background loop hasn't ticked yet
        self.record_sample(
            online=reachable,
            busy=busy and reachable,
            latest_chat_ms=gateway_chat_ms,
            now_ts=ts,
            force=False,
        )

        with self._lock:
            uptime_7d = self._compute_7d_uptime_locked(ts)
            heartbeat_ticks = list(self._recent_ticks[-HISTORY_BAR_COUNT:])

        # Make the rightmost tick reflect instantaneous real-time status
        live_state = "down" if not reachable else ("busy" if busy else "up")
        if heartbeat_ticks:
            heartbeat_ticks[-1] = {
                "ts": int(ts),
                "state": live_state,
                "chat_ms": gateway_chat_ms or heartbeat_ticks[-1].get("chat_ms"),
            }
        padded_gateway_ticks = self._pad_ticks(heartbeat_ticks, HISTORY_BAR_COUNT)

        clean_origin = (public_origin or "").strip().rstrip("/")
        if clean_origin.endswith("/v1"):
            clean_origin = clean_origin[:-3].rstrip("/")
        probe_url = f"{clean_origin}/status/ping" if clean_origin else "/status/ping"
        public_ready = bool(clean_origin) and not _is_local_origin(clean_origin)
        ext_urls = build_external_speedtest_urls(probe_url if public_ready else "")

        monitors: List[Dict[str, Any]] = []
        by_model = ledger_stats["by_model"]
        for spec in MONITORED_MODELS:
            is_primary = bool(spec.get("is_primary"))
            m_stat = by_model.get(spec["canonical"]) or {}
            if not reachable:
                card_state = "down"
            elif busy:
                card_state = "busy"
            elif m_stat.get("last_state") == "down":
                card_state = "degraded"
            else:
                card_state = "up"

            m_chat_ms = m_stat.get("chat_latency_ms") or gateway_chat_ms
            m_uptime = (
                uptime_7d
                if m_stat.get("success_rate_7d") is None
                else float(m_stat["success_rate_7d"])
            )
            raw_ticks = m_stat["call_ticks"] if m_stat.get("call_ticks") else heartbeat_ticks
            if raw_ticks and not reachable:
                raw_ticks = list(raw_ticks[:-1]) + [{"ts": int(ts), "state": "down", "chat_ms": None}]

            monitors.append(
                {
                    "id": spec["id"],
                    "title": spec["title"],
                    "provider": spec["provider"],
                    "pool": spec["pool"],
                    "is_primary": is_primary,
                    "state": card_state,
                    "state_label": {
                        "up": "正常",
                        "busy": "繁忙排队",
                        "degraded": "波动",
                        "down": "离线",
                    }.get(card_state, "正常"),
                    "chat_latency_ms": m_chat_ms,
                    "chat_latency_source": "passive_real" if m_chat_ms is not None else "awaiting_traffic",
                    "uptime_7d_pct": m_uptime,
                    "ticks": self._pad_ticks(raw_ticks, HISTORY_BAR_COUNT),
                }
            )

        if not online:
            overall_label = "上游连接离线"
            tunnel_state = "down"
        elif local_only_view:
            overall_label = "本机服务正常 · 公网通道未开启"
            tunnel_state = "local_only"
        elif tunnel_probe_failed:
            overall_label = "本机服务正常 · 公网连接异常"
            tunnel_state = "down"
        elif busy:
            overall_label = "高负载排队中"
            tunnel_state = "busy"
        else:
            overall_label = "服务运行正常"
            tunnel_state = "up"

        return {
            "status": "ok" if reachable else "offline",
            "online": reachable,
            "busy": busy and reachable,
            "tunnel_state": tunnel_state,
            "overall_label": overall_label,
            "updated_at": int(ts),
            "refresh_interval_sec": CLIENT_REFRESH_SEC,
            "history_bar_count": HISTORY_BAR_COUNT,
            "public_origin": clean_origin,
            "api_base_url": f"{clean_origin}/v1" if clean_origin else "/v1",
            "status_page_url": f"{clean_origin}/status" if clean_origin else "/status",
            "probe_url": probe_url,
            "is_public_origin": public_ready,
            "itdog_url": ext_urls["itdog_url"],
            "chinaz_url": ext_urls["chinaz_url"],
            "boce_url": ext_urls["boce_url"],
            "network_map": self.get_cached_network_map(clean_origin),
            "server_probe_ms": server_probe_ms,
            "last_chat_sampled_at": ledger_stats["last_chat_ts"],
            "monitors": monitors,
            "gateway_ticks": padded_gateway_ticks,
        }

    def get_cached_network_map(self, public_origin: str) -> Optional[Dict[str, Any]]:
        clean = (public_origin or "").strip().rstrip("/")
        if clean.endswith("/v1"):
            clean = clean[:-3].rstrip("/")
        with self._lock:
            cached = self._network_map_cache
            if not cached:
                return None
            if clean and cached.get("origin") and cached.get("origin") != clean:
                return None
            return dict(cached)

    @staticmethod
    def _execute_globalping_http_probe(public_origin: str, probe_path: str = "/status/ping") -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(public_origin)
        scheme = (parsed.scheme or "https").upper()
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "HTTPS" else 80)
        if not hostname or _is_local_origin(hostname):
            raise ValueError("公网隧道尚未开启，本机回环地址无法从外部多省节点拨测。")

        ctx = ssl.create_default_context()
        headers = {
            "User-Agent": "GeminiEnterpriseBridge-StatusMonitor/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req_payload = {
            "type": "http",
            "target": hostname,
            "measurementOptions": {
                "protocol": "HTTPS" if scheme == "HTTPS" else "HTTP",
                "port": port,
                "request": {
                    "method": "GET",
                    "path": probe_path,
                },
            },
            "locations": [
                {"country": "CN", "asn": 4134, "limit": 4},
                {"country": "CN", "asn": 4837, "limit": 4},
                {"country": "CN", "asn": 9808, "limit": 4},
                {"country": "HK", "limit": 2},
                {"country": "TW", "limit": 1},
                {"country": "JP", "limit": 1},
                {"country": "SG", "limit": 1},
                {"country": "US", "limit": 1},
            ],
        }

        create_req = urllib.request.Request(
            GLOBALPING_API_BASE,
            data=json.dumps(req_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(create_req, timeout=10.0, context=ctx) as resp:
            created = json.loads(resp.read().decode("utf-8", "ignore"))
        measurement_id = str(created.get("id") or "").strip()
        if not measurement_id:
            raise RuntimeError("未能创建分布式拨测任务")

        poll_url = f"{GLOBALPING_API_BASE}/{measurement_id}"
        final_data: Dict[str, Any] = {}
        for _ in range(12):
            time.sleep(1.1)
            poll_req = urllib.request.Request(
                poll_url,
                headers={"User-Agent": headers["User-Agent"], "Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(poll_req, timeout=8.0, context=ctx) as p_resp:
                final_data = json.loads(p_resp.read().decode("utf-8", "ignore"))
            if final_data.get("status") != "in-progress":
                break

        nodes: List[Dict[str, Any]] = []
        isp_buckets: Dict[str, List[int]] = {
            "telecom": [],
            "unicom": [],
            "mobile": [],
            "overseas": [],
        }
        ok_count = 0
        raw_results = final_data.get("results") or []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            probe_info = item.get("probe") or {}
            res_info = item.get("result") or {}
            country = str(probe_info.get("country") or "").upper()
            city = str(probe_info.get("city") or "")
            asn = int(probe_info.get("asn") or 0)
            network = str(probe_info.get("network") or "")
            lat = probe_info.get("latitude")
            lon = probe_info.get("longitude")
            isp_key, isp_label = _classify_isp(country, asn, network)
            region_zh = _localize_probe_region(country, city)

            timings = res_info.get("timings") or {}
            status_str = str(res_info.get("status") or "failed")
            http_code = int(res_info.get("statusCode") or 0)
            total_ms = timings.get("total")
            tcp_ms = timings.get("tcp")
            tls_ms = timings.get("tls")
            ttfb_ms = timings.get("firstByte")
            edge_ms = (
                int(tcp_ms or 0) + int(tls_ms or 0)
                if (tcp_ms is not None or tls_ms is not None)
                else None
            )

            reachable = status_str == "finished" and http_code in {200, 204}
            if reachable and isinstance(total_ms, (int, float)) and total_ms > 0:
                ok_count += 1
                t_int = int(round(total_ms))
                if isp_key in {"telecom", "unicom", "mobile"}:
                    isp_buckets[isp_key].append(t_int)
                elif country != "CN":
                    isp_buckets["overseas"].append(t_int)
            else:
                t_int = int(round(total_ms)) if isinstance(total_ms, (int, float)) and total_ms > 0 else None

            if reachable:
                node_state = "fast" if (t_int and t_int <= 1000) else ("good" if (t_int and t_int <= 1800) else "slow")
            elif http_code == 503:
                node_state = "offline"
            else:
                node_state = "timeout"

            nodes.append(
                {
                    "country": country,
                    "city": city,
                    "region_zh": region_zh,
                    "isp_key": isp_key,
                    "isp_label": isp_label,
                    "network": network,
                    "lat": float(lat) if isinstance(lat, (int, float)) else None,
                    "lon": float(lon) if isinstance(lon, (int, float)) else None,
                    "http_code": http_code,
                    "state": node_state,
                    "edge_ms": edge_ms,
                    "ttfb_ms": int(round(ttfb_ms)) if isinstance(ttfb_ms, (int, float)) else None,
                    "total_ms": t_int,
                }
            )

        nodes.sort(
            key=lambda n: (
                0 if n["country"] == "CN" else 1,
                0 if n["total_ms"] is not None else 1,
                n["total_ms"] if n["total_ms"] is not None else 999999,
            )
        )

        def _avg(vals: List[int]) -> Optional[int]:
            return int(round(statistics.mean(vals))) if vals else None

        total_nodes = len(nodes)
        return {
            "ok": True,
            "measurement_id": measurement_id,
            "origin": public_origin.rstrip("/"),
            "probe_url": f"{public_origin.rstrip('/')}{probe_path}",
            "tested_at": int(time.time()),
            "cache_ttl_sec": NETWORK_MAP_CACHE_TTL_SEC,
            "summary": {
                "total_nodes": total_nodes,
                "reachable_nodes": ok_count,
                "reachability_pct": round((ok_count / total_nodes) * 100.0, 1) if total_nodes else 0.0,
                "telecom_avg_ms": _avg(isp_buckets["telecom"]),
                "unicom_avg_ms": _avg(isp_buckets["unicom"]),
                "mobile_avg_ms": _avg(isp_buckets["mobile"]),
                "overseas_avg_ms": _avg(isp_buckets["overseas"]),
            },
            "nodes": nodes,
        }

    async def run_network_map_probe(self, public_origin: str, *, force: bool = False) -> Dict[str, Any]:
        clean = (public_origin or "").strip().rstrip("/")
        if clean.endswith("/v1"):
            clean = clean[:-3].rstrip("/")
        if not clean or _is_local_origin(clean):
            return {
                "ok": False,
                "detail": "当前尚未开启公网通道（本机 127.0.0.1 无法从全国外网节点直接拨测），开启一键公网后即可实测。",
            }

        now = time.time()
        with self._lock:
            if (
                not force
                and self._network_map_cache
                and self._network_map_cache.get("origin") == clean
                and (now - float(self._network_map_cache.get("tested_at") or 0)) < NETWORK_MAP_CACHE_TTL_SEC
            ):
                cached_copy = dict(self._network_map_cache)
                cached_copy["cached"] = True
                return cached_copy

        async with self._network_map_async_lock:
            now2 = time.time()
            with self._lock:
                if (
                    not force
                    and self._network_map_cache
                    and self._network_map_cache.get("origin") == clean
                    and (now2 - float(self._network_map_cache.get("tested_at") or 0)) < NETWORK_MAP_CACHE_TTL_SEC
                ):
                    cached_copy = dict(self._network_map_cache)
                    cached_copy["cached"] = True
                    return cached_copy

            result = await asyncio.to_thread(self._execute_globalping_http_probe, clean, "/status/ping")
            with self._lock:
                self._network_map_cache = result
            out = dict(result)
            out["cached"] = False
            return out


def get_public_status_html() -> str:
    """Return standalone, zero-CDN public availability monitor HTML."""
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI 接口可用性监控 · 实时状态与全国延迟</title>
  <style>
    :root {
      --bg: #0b1120;
      --bg-card: #111a2e;
      --bg-sub: #0d1526;
      --border: rgba(148, 163, 184, 0.14);
      --border-hover: rgba(56, 189, 248, 0.35);
      --text: #f1f5f9;
      --muted: #94a3b8;
      --dim: #64748b;
      --green: #10b981;
      --green-soft: rgba(16, 185, 129, 0.14);
      --amber: #f59e0b;
      --amber-soft: rgba(245, 158, 11, 0.15);
      --red: #ef4444;
      --red-soft: rgba(239, 68, 68, 0.15);
      --blue: #38bdf8;
      --blue-soft: rgba(56, 189, 248, 0.14);
      --bar-empty: rgba(148, 163, 184, 0.16);
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at 15% 0%, rgba(16, 185, 129, 0.08), transparent 42%),
                  radial-gradient(circle at 85% 5%, rgba(56, 189, 248, 0.08), transparent 40%),
                  var(--bg);
      color: var(--text);
      min-height: 100vh;
      line-height: 1.5;
    }
    .wrap {
      max-width: 1160px;
      margin: 0 auto;
      padding: 28px 20px 56px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      margin-bottom: 22px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-logo {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.22), rgba(56, 189, 248, 0.22));
      border: 1px solid rgba(56, 189, 248, 0.28);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      font-weight: 700;
      color: #34d399;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    }
    .brand-title {
      margin: 0;
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .brand-sub {
      font-size: 0.82rem;
      color: var(--muted);
      margin-top: 2px;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 13px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 600;
      background: var(--green-soft);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .status-pill.busy {
      background: var(--amber-soft);
      color: #fbbf24;
      border-color: rgba(245, 158, 11, 0.35);
    }
    .status-pill.down, .status-pill.degraded {
      background: var(--red-soft);
      color: #f87171;
      border-color: rgba(239, 68, 68, 0.35);
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
    }
    .btn {
      appearance: none;
      border: 1px solid var(--border);
      background: rgba(30, 41, 59, 0.65);
      color: var(--text);
      font-size: 0.82rem;
      font-weight: 600;
      padding: 8px 14px;
      border-radius: 10px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: border-color 0.16s, background 0.16s, transform 0.1s;
    }
    .btn:hover {
      border-color: var(--border-hover);
      background: rgba(30, 41, 59, 0.95);
    }
    .btn:active { transform: translateY(1px); }
    .btn.primary {
      background: linear-gradient(135deg, #059669, #0284c7);
      border-color: rgba(56, 189, 248, 0.4);
      color: #ffffff;
    }
    .btn.primary:hover {
      filter: brightness(1.08);
    }

    /* Nationwide Map & Live Ping Hero Banner */
    .map-banner {
      background: linear-gradient(145deg, rgba(17, 26, 46, 0.96), rgba(13, 21, 38, 0.96));
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px 20px;
      margin-bottom: 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
    }
    .map-info {
      flex: 1 1 420px;
      min-width: 260px;
    }
    .map-title-row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .map-title {
      font-size: 1rem;
      font-weight: 700;
      margin: 0;
    }
    .isp-tags {
      display: inline-flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .isp-tag {
      font-size: 0.72rem;
      padding: 2px 8px;
      border-radius: 6px;
      background: rgba(56, 189, 248, 0.1);
      color: #7dd3fc;
      border: 1px solid rgba(56, 189, 248, 0.2);
    }
    .map-desc {
      font-size: 0.82rem;
      color: var(--muted);
      margin: 0 0 10px;
    }
    .legend-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      font-size: 0.75rem;
      color: var(--muted);
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 3px;
      display: inline-block;
    }
    .map-actions {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
    }
    .map-btn-group {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .my-ping-note {
      font-size: 0.78rem;
      color: var(--muted);
    }
    .my-ping-note strong {
      color: #34d399;
      font-variant-numeric: tabular-nums;
    }
    .map-live-drawer {
      width: 100%;
      margin-top: 14px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    .isp-kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .isp-kpi-box {
      background: var(--bg-sub);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
    }
    .isp-kpi-lbl {
      font-size: 0.73rem;
      color: var(--muted);
    }
    .isp-kpi-val {
      font-size: 1.05rem;
      font-weight: 700;
      color: #34d399;
      margin-top: 2px;
      font-variant-numeric: tabular-nums;
    }
    .map-split {
      display: grid;
      grid-template-columns: minmax(290px, 1.05fr) minmax(300px, 1.25fr);
      gap: 14px;
      align-items: stretch;
    }
    .china-svg-box {
      background: var(--bg-sub);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .china-svg {
      width: 100%;
      height: auto;
      max-height: 310px;
      display: block;
    }
    .probe-table-wrap {
      background: var(--bg-sub);
      border: 1px solid var(--border);
      border-radius: 12px;
      max-height: 350px;
      overflow-y: auto;
    }
    .probe-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.78rem;
    }
    .probe-table th {
      position: sticky;
      top: 0;
      background: #0f172a;
      color: var(--muted);
      text-align: left;
      padding: 8px 10px;
      font-weight: 600;
      border-bottom: 1px solid var(--border);
    }
    .probe-table td {
      padding: 7px 10px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.08);
      font-variant-numeric: tabular-nums;
    }
    .isp-mini {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 5px;
      font-size: 0.7rem;
      background: rgba(56, 189, 248, 0.12);
      color: #7dd3fc;
    }
    .ext-verify-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px dashed rgba(148, 163, 184, 0.14);
      font-size: 0.75rem;
      color: var(--muted);
    }
    @media (max-width: 820px) {
      .map-split { grid-template-columns: 1fr; }
    }

    /* Section Header matching RelayPulse */
    .sec-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 8px 0 14px;
    }
    .sec-head-left {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .sec-name {
      font-size: 1.05rem;
      font-weight: 700;
    }
    .count-chip {
      font-size: 0.75rem;
      padding: 2px 9px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.14);
      color: var(--muted);
      font-weight: 600;
    }
    .sec-note {
      font-size: 0.78rem;
      color: var(--dim);
    }

    /* Cards Grid */
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(335px, 1fr));
      gap: 16px;
    }
    .mon-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      transition: border-color 0.18s, box-shadow 0.18s;
      position: relative;
    }
    .mon-card.primary-card {
      border-color: rgba(16, 185, 129, 0.28);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.22);
    }
    .mon-card:hover {
      border-color: rgba(148, 163, 184, 0.3);
    }
    .card-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 14px;
    }
    .card-ident {
      display: flex;
      align-items: center;
      gap: 11px;
      min-width: 0;
    }
    .card-icon {
      width: 38px;
      height: 38px;
      border-radius: 10px;
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.22);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      flex-shrink: 0;
    }
    .card-name {
      font-size: 0.98rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .card-tags {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.74rem;
      color: var(--muted);
      margin-top: 2px;
      flex-wrap: wrap;
    }
    .tag-sep { color: var(--dim); }

    .metrics-pair {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric-box {
      background: var(--bg-sub);
      border: 1px solid rgba(148, 163, 184, 0.1);
      border-radius: 11px;
      padding: 10px 12px;
    }
    .metric-label {
      font-size: 0.74rem;
      color: var(--muted);
      display: flex;
      align-items: center;
      gap: 5px;
      margin-bottom: 4px;
    }
    .metric-val {
      font-size: 1.15rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: #34d399;
    }
    .metric-val.warn { color: #fbbf24; }
    .metric-val.muted { color: var(--muted); font-size: 0.92rem; font-weight: 600; }

    .uptime-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      margin-bottom: 4px;
      font-size: 0.82rem;
    }
    .uptime-label { color: var(--muted); font-weight: 600; }
    .uptime-pct {
      font-size: 1rem;
      font-weight: 700;
      color: #34d399;
      font-variant-numeric: tabular-nums;
    }
    .uptime-sub {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.73rem;
      color: var(--dim);
      margin-bottom: 8px;
    }
    .bars-row {
      display: grid;
      grid-template-columns: repeat(60, minmax(0, 1fr));
      gap: 2px;
      height: 28px;
      align-items: stretch;
    }
    .tick-bar {
      border-radius: 3px;
      background: var(--bar-empty);
      transition: transform 0.12s, opacity 0.12s;
      cursor: pointer;
    }
    .tick-bar:hover {
      transform: scaleY(1.14);
      opacity: 0.92;
    }
    .tick-bar.up { background: var(--green); }
    .tick-bar.busy, .tick-bar.degraded { background: var(--amber); }
    .tick-bar.down { background: var(--red); }
    .bars-foot {
      display: flex;
      justify-content: space-between;
      font-size: 0.68rem;
      letter-spacing: 0.06em;
      color: var(--dim);
      margin-top: 6px;
      font-weight: 600;
    }

    /* Endpoint Quick Copy Bar */
    .endpoint-footer {
      margin-top: 24px;
      background: rgba(17, 26, 46, 0.72);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .ep-code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.84rem;
      color: #7dd3fc;
      word-break: break-all;
    }
    .tooltip {
      position: fixed;
      z-index: 999;
      pointer-events: none;
      background: rgba(15, 23, 42, 0.96);
      border: 1px solid rgba(148, 163, 184, 0.28);
      color: var(--text);
      padding: 6px 10px;
      border-radius: 8px;
      font-size: 0.75rem;
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4);
      white-space: nowrap;
    }
    @media (max-width: 640px) {
      .map-actions { align-items: flex-start; width: 100%; }
      .cards-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="topbar">
      <div class="brand">
        <div class="brand-logo">✦</div>
        <div>
          <h1 class="brand-title">AI 接口可用性与延迟监控</h1>
          <div class="brand-sub">OpenAI 兼容网关 · 实时健康心跳与全国各省访问测速</div>
        </div>
      </div>
      <div class="top-actions">
        <span class="status-pill" id="overallPill"><span class="dot"></span><span id="overallText">检测中...</span></span>
        <button type="button" class="btn" id="manualRefreshBtn">↻ 立即刷新 (<span id="topCountdown">45</span>s)</button>
      </div>
    </header>

    <section class="map-banner" id="network-map">
      <div class="map-info">
        <div class="map-title-row">
          <h2 class="map-title">🗺️ 全国各省可用性与三网延迟地图</h2>
          <div class="isp-tags">
            <span class="isp-tag">电信</span>
            <span class="isp-tag">联通</span>
            <span class="isp-tag">移动</span>
            <span class="isp-tag">港澳台 / 海外</span>
          </div>
        </div>
        <p class="map-desc" id="mapDescText" hidden></p>
        <div class="legend-row">
          <span class="legend-item"><span class="swatch" style="background:#10b981"></span>&lt;=1.0s 极速</span>
          <span class="legend-item"><span class="swatch" style="background:#34d399"></span>1.0s-1.8s 流畅</span>
          <span class="legend-item"><span class="swatch" style="background:#fbbf24"></span>1.8s-3.5s 正常</span>
          <span class="legend-item"><span class="swatch" style="background:#ef4444"></span>超时 / 503 离线</span>
        </div>
      </div>
      <div class="map-actions">
        <div class="map-btn-group">
          <button type="button" class="btn primary" id="runMapProbeBtn">⚡ 立即实测全国三网与海外</button>
          <button type="button" class="btn" id="copyProbeBtn">复制测速探针地址</button>
        </div>
        <div class="my-ping-note">
          您当前网络直连网关延迟：<strong id="myLivePing">测速中...</strong>
        </div>
      </div>

      <div class="map-live-drawer" id="liveMapDrawer" hidden>
        <div class="isp-kpi-grid" id="ispKpiGrid"></div>
        <div class="map-split">
          <div class="china-svg-box">
            <svg class="china-svg" id="chinaProbeSvg" viewBox="0 0 560 360" xmlns="http://www.w3.org/2000/svg"></svg>
            <div style="display:flex;justify-content:space-between;font-size:0.72rem;color:var(--dim);margin-top:6px;">
              <span id="mapMetaLeft">真实物理探针端到端 HTTPS 实测</span>
              <span id="mapMetaRight">悬停圆点查看详情</span>
            </div>
          </div>
          <div class="probe-table-wrap">
            <table class="probe-table">
              <thead>
                <tr>
                  <th>节点地区</th>
                  <th>运营商</th>
                  <th>边缘握手</th>
                  <th>端到端总延迟</th>
                </tr>
              </thead>
              <tbody id="probeTableBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="ext-verify-bar">
        <span>第三方权威多省家宽交叉验证（点击 ITDOG 自动复制探针网址，粘贴即测）：</span>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" class="btn" id="openItdogBtn" style="padding:5px 10px;font-size:0.75rem;">🔥 ITDOG 全国三网家宽</button>
          <a class="btn" id="openChinazBtn" href="https://tool.chinaz.com/speedtest" target="_blank" rel="noopener noreferrer" style="padding:5px 10px;font-size:0.75rem;">站长工具 Chinaz</a>
          <a class="btn" id="openBoceBtn" href="https://www.boce.com/http" target="_blank" rel="noopener noreferrer" style="padding:5px 10px;font-size:0.75rem;">拨测 Boce 地图</a>
        </div>
      </div>
    </section>

    <div class="sec-head">
      <div class="sec-head-left">
        <span class="sec-name">OpenAI 兼容接口</span>
        <span class="count-chip" id="monitorCountChip">4 个监控</span>
      </div>
      <span class="sec-note">每分钟零消耗检测网关在线状态 · 对话延迟取自真实调用</span>
    </div>

    <main class="cards-grid" id="cardsGrid"></main>

    <footer class="endpoint-footer">
      <div>
        <div style="font-size:0.76rem;color:var(--muted);margin-bottom:2px;">客户端填写的接口地址 (Base URL · 需配合专属密钥使用)</div>
        <div class="ep-code" id="footerApiBase">加载中...</div>
      </div>
      <button type="button" class="btn" id="copyApiBaseBtn">复制接口地址</button>
    </footer>
  </div>

  <div class="tooltip" id="barTooltip" hidden></div>

  <script>
    (function () {
      var refreshSec = 45;
      var remainSec = 45;
      var latestProbeUrl = '';
      var latestApiBase = '';
      var lastBrowserPingMs = null;
      var mapProbeRunning = false;
      var tooltipEl = document.getElementById('barTooltip');

      var CHINA_OUTLINE_LONLAT = [
        [134.5,48.4],[131.2,44.9],[128.0,41.5],[124.3,39.9],[121.6,39.0],[122.5,37.2],
        [119.5,35.2],[121.8,31.2],[121.5,28.5],[119.6,25.5],[117.0,23.5],[114.2,22.5],
        [110.2,20.2],[108.1,21.5],[105.8,22.8],[102.5,22.6],[98.8,24.0],[97.5,28.2],
        [92.0,27.8],[88.8,27.5],[81.2,30.2],[78.5,32.8],[75.0,37.2],[74.2,39.8],
        [80.0,42.2],[83.0,45.8],[87.6,49.1],[92.5,47.5],[96.8,42.8],[105.0,42.0],
        [111.8,43.8],[116.5,47.8],[119.8,49.9],[123.5,53.3],[127.5,51.2],[130.8,48.8],[134.5,48.4]
      ];

      function projectChinaPoint(lon, lat, country) {
        if (country && country !== 'CN' && country !== 'HK' && country !== 'MO' && country !== 'TW') {
          if (country === 'JP') return { x: 515, y: 122 };
          if (country === 'KR') return { x: 486, y: 132 };
          if (country === 'SG') return { x: 372, y: 334 };
          if (country === 'US') return { x: 518, y: 58 };
          return { x: 52, y: 60 };
        }
        var x = ((lon - 73.0) / (136.0 - 73.0)) * 450 + 28;
        var y = ((54.0 - lat) / (54.0 - 17.5)) * 305 + 22;
        return {
          x: Math.max(18, Math.min(540, Math.round(x * 10) / 10)),
          y: Math.max(18, Math.min(342, Math.round(y * 10) / 10))
        };
      }

      function colorForNode(nd) {
        if (!nd || nd.state === 'offline' || nd.state === 'timeout' || !nd.total_ms) return '#ef4444';
        if (nd.total_ms <= 1000) return '#10b981';
        if (nd.total_ms <= 1800) return '#34d399';
        return '#fbbf24';
      }

      function renderNetworkMap(mapData) {
        var drawer = document.getElementById('liveMapDrawer');
        if (!mapData || !mapData.ok || !Array.isArray(mapData.nodes)) return;
        drawer.hidden = false;

        var kpiGrid = document.getElementById('ispKpiGrid');
        kpiGrid.textContent = '';
        var sum = mapData.summary || {};
        var kpis = [
          { lbl: '实测连通率', val: (sum.reachable_nodes || 0) + '/' + (sum.total_nodes || 0) + ' (' + (sum.reachability_pct || 0) + '%)' },
          { lbl: '中国电信端到端均值', val: sum.telecom_avg_ms ? (sum.telecom_avg_ms + ' ms') : '本轮未命中' },
          { lbl: '中国联通端到端均值', val: sum.unicom_avg_ms ? (sum.unicom_avg_ms + ' ms') : '本轮未命中' },
          { lbl: '中国移动端到端均值', val: sum.mobile_avg_ms ? (sum.mobile_avg_ms + ' ms') : '本轮未命中' },
          { lbl: '港澳台/海外均值', val: sum.overseas_avg_ms ? (sum.overseas_avg_ms + ' ms') : '--' }
        ];
        kpis.forEach(function (k) {
          var box = document.createElement('div');
          box.className = 'isp-kpi-box';
          var l = document.createElement('div');
          l.className = 'isp-kpi-lbl';
          l.textContent = k.lbl;
          var v = document.createElement('div');
          v.className = 'isp-kpi-val';
          v.textContent = k.val;
          box.appendChild(l);
          box.appendChild(v);
          kpiGrid.appendChild(box);
        });

        var svg = document.getElementById('chinaProbeSvg');
        var ns = 'http://www.w3.org/2000/svg';
        svg.textContent = '';

        var pts = CHINA_OUTLINE_LONLAT.map(function (pair) {
          var p = projectChinaPoint(pair[0], pair[1], 'CN');
          return p.x + ',' + p.y;
        }).join(' ');
        var poly = document.createElementNS(ns, 'polygon');
        poly.setAttribute('points', pts);
        poly.setAttribute('fill', 'rgba(30, 41, 59, 0.72)');
        poly.setAttribute('stroke', 'rgba(56, 189, 248, 0.32)');
        poly.setAttribute('stroke-width', '1.4');
        svg.appendChild(poly);

        var usedCoords = {};
        mapData.nodes.forEach(function (nd, idx) {
          if (nd.lon === null || nd.lat === null) return;
          var pt = projectChinaPoint(nd.lon, nd.lat, nd.country);
          var key = Math.round(pt.x / 8) + ':' + Math.round(pt.y / 8);
          var jitter = usedCoords[key] || 0;
          usedCoords[key] = jitter + 1;
          var cx = pt.x + (jitter * 7);
          var cy = pt.y - (jitter * 5);
          var col = colorForNode(nd);

          var halo = document.createElementNS(ns, 'circle');
          halo.setAttribute('cx', String(cx));
          halo.setAttribute('cy', String(cy));
          halo.setAttribute('r', '10');
          halo.setAttribute('fill', col);
          halo.setAttribute('opacity', '0.22');
          svg.appendChild(halo);

          var dot = document.createElementNS(ns, 'circle');
          dot.setAttribute('cx', String(cx));
          dot.setAttribute('cy', String(cy));
          dot.setAttribute('r', '4.8');
          dot.setAttribute('fill', col);
          dot.setAttribute('stroke', '#0b1120');
          dot.setAttribute('stroke-width', '1.3');
          dot.style.cursor = 'pointer';

          var tipText = nd.region_zh + ' (' + nd.isp_label + ') · '
            + (nd.total_ms ? ('端到端 ' + nd.total_ms + 'ms') : ('异常 HTTP ' + (nd.http_code || '超时')))
            + (nd.edge_ms ? (' · 边缘握手 ' + nd.edge_ms + 'ms') : '');
          dot.addEventListener('mouseenter', function (ev) { showTooltip(ev, tipText); });
          dot.addEventListener('mousemove', function (ev) { showTooltip(ev, tipText); });
          dot.addEventListener('mouseleave', hideTooltip);
          svg.appendChild(dot);

          if (jitter === 0 && idx < 12) {
            var lbl = document.createElementNS(ns, 'text');
            lbl.setAttribute('x', String(cx + 7));
            lbl.setAttribute('y', String(cy + 3));
            lbl.setAttribute('fill', '#cbd5e1');
            lbl.setAttribute('font-size', '10');
            lbl.textContent = nd.region_zh.replace(/^.*·/, '');
            svg.appendChild(lbl);
          }
        });

        var tbody = document.getElementById('probeTableBody');
        tbody.textContent = '';
        mapData.nodes.forEach(function (nd) {
          var tr = document.createElement('tr');
          var tdReg = document.createElement('td');
          tdReg.textContent = nd.region_zh;
          var tdIsp = document.createElement('td');
          var badge = document.createElement('span');
          badge.className = 'isp-mini';
          badge.textContent = nd.isp_label;
          tdIsp.appendChild(badge);
          var tdEdge = document.createElement('td');
          tdEdge.style.color = 'var(--muted)';
          tdEdge.textContent = nd.edge_ms ? (nd.edge_ms + ' ms') : '--';
          var tdTot = document.createElement('td');
          tdTot.style.fontWeight = '700';
          tdTot.style.color = colorForNode(nd);
          if (nd.total_ms && nd.http_code === 200) {
            tdTot.textContent = nd.total_ms + ' ms (200)';
          } else if (nd.http_code === 503) {
            tdTot.textContent = '离线 (503)';
          } else {
            tdTot.textContent = nd.http_code ? ('HTTP ' + nd.http_code) : '连接超时';
          }
          tr.appendChild(tdReg);
          tr.appendChild(tdIsp);
          tr.appendChild(tdEdge);
          tr.appendChild(tdTot);
          tbody.appendChild(tr);
        });

        document.getElementById('mapMetaLeft').textContent =
          'Globalping 真实物理探针实测 · 更新于 ' + fmtClock(mapData.tested_at);
      }

      async function triggerLiveNetworkMap(forceRun) {
        if (mapProbeRunning) return;
        var btn = document.getElementById('runMapProbeBtn');
        mapProbeRunning = true;
        var origLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ 正在调度全国三网与海外物理节点实测 (约 4-6s)...';
        try {
          var resp = await fetch('/api/public/network-map' + (forceRun ? '?force=1' : ''), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
          });
          var d = await resp.json();
          var descEl = document.getElementById('mapDescText');
          if (!resp.ok || !d.ok) {
            if (descEl) {
              descEl.hidden = false;
              descEl.textContent = (d && d.detail) ? d.detail : '多节点拨测暂不可用，请稍后重试或使用下方 ITDOG 测速。';
            }
            return;
          }
          if (descEl) {
            descEl.hidden = true;
            descEl.textContent = '';
          }
          renderNetworkMap(d);
        } catch (err) {
          var errEl = document.getElementById('mapDescText');
          if (errEl) {
            errEl.hidden = false;
            errEl.textContent = '拨测请求异常：' + String(err.message || err);
          }
        } finally {
          mapProbeRunning = false;
          btn.disabled = false;
          btn.textContent = origLabel;
        }
      }

      function fmtChatMs(ms) {
        if (ms === null || ms === undefined || ms <= 0) return '待采样';
        if (ms >= 10000) return (ms / 1000).toFixed(1) + 's (' + ms + 'ms)';
        return ms + 'ms';
      }

      function fmtClock(ts) {
        if (!ts || ts <= 0) return '尚未采样';
        var d = new Date(ts * 1000);
        var pad = function (n) { return n < 10 ? '0' + n : String(n); };
        return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
      }

      function stateLabel(st) {
        if (st === 'up') return '正常';
        if (st === 'busy') return '高负载排队';
        if (st === 'degraded') return '轻微波动';
        if (st === 'down') return '离线 / 异常';
        return '待采样';
      }

      async function measureLivePing() {
        var t0 = performance.now();
        try {
          var r = await fetch('/status/ping?_=' + Date.now(), { cache: 'no-store' });
          var rtt = Math.max(1, Math.round(performance.now() - t0));
          if (r.ok || r.status === 503) {
            lastBrowserPingMs = rtt;
          }
        } catch (_) {
          lastBrowserPingMs = null;
        }
        var pingEl = document.getElementById('myLivePing');
        if (pingEl) {
          pingEl.textContent = lastBrowserPingMs !== null ? (lastBrowserPingMs + ' ms') : '连接超时';
          pingEl.style.color = (lastBrowserPingMs !== null && lastBrowserPingMs <= 500) ? '#34d399' : '#fbbf24';
        }
        return lastBrowserPingMs;
      }

      function showTooltip(ev, text) {
        tooltipEl.textContent = text;
        tooltipEl.hidden = false;
        var x = Math.min(window.innerWidth - tooltipEl.offsetWidth - 12, Math.max(8, ev.clientX - 40));
        var y = Math.max(8, ev.clientY - 42);
        tooltipEl.style.left = x + 'px';
        tooltipEl.style.top = y + 'px';
      }

      function hideTooltip() {
        tooltipEl.hidden = true;
      }

      function renderCards(data, pingMs) {
        var grid = document.getElementById('cardsGrid');
        grid.textContent = '';
        var list = Array.isArray(data.monitors) ? data.monitors : [];
        document.getElementById('monitorCountChip').textContent = list.length + ' 个监控';

        list.forEach(function (m) {
          var card = document.createElement('article');
          card.className = 'mon-card' + (m.is_primary ? ' primary-card' : '');

          var top = document.createElement('div');
          top.className = 'card-top';

          var ident = document.createElement('div');
          ident.className = 'card-ident';
          var icon = document.createElement('div');
          icon.className = 'card-icon';
          icon.textContent = m.is_primary ? '⚡' : '✦';

          var titleWrap = document.createElement('div');
          titleWrap.style.minWidth = '0';
          var nameEl = document.createElement('div');
          nameEl.className = 'card-name';
          nameEl.textContent = m.title || m.id;
          var tagsEl = document.createElement('div');
          tagsEl.className = 'card-tags';
          tagsEl.textContent = (m.provider || 'OpenAI') + '  |  ' + (m.pool || '号池');
          titleWrap.appendChild(nameEl);
          titleWrap.appendChild(tagsEl);
          ident.appendChild(icon);
          ident.appendChild(titleWrap);

          var badge = document.createElement('span');
          badge.className = 'status-pill ' + (m.state || 'up');
          var dot = document.createElement('span');
          dot.className = 'dot';
          var bTxt = document.createElement('span');
          bTxt.textContent = m.state_label || stateLabel(m.state);
          badge.appendChild(dot);
          badge.appendChild(bTxt);

          top.appendChild(ident);
          top.appendChild(badge);
          card.appendChild(top);

          var pair = document.createElement('div');
          pair.className = 'metrics-pair';

          var boxChat = document.createElement('div');
          boxChat.className = 'metric-box';
          var lblChat = document.createElement('div');
          lblChat.className = 'metric-label';
          lblChat.textContent = '💬 对话延迟';
          var valChat = document.createElement('div');
          var cMs = m.chat_latency_ms;
          valChat.className = 'metric-val' + (!cMs ? ' muted' : (cMs > 20000 ? ' warn' : ''));
          valChat.textContent = fmtChatMs(cMs);
          boxChat.appendChild(lblChat);
          boxChat.appendChild(valChat);

          var boxPing = document.createElement('div');
          boxPing.className = 'metric-box';
          var lblPing = document.createElement('div');
          lblPing.className = 'metric-label';
          lblPing.textContent = '⚡ 端点 PING';
          var valPing = document.createElement('div');
          var effPing = pingMs !== null ? pingMs : data.server_probe_ms;
          valPing.className = 'metric-val' + (!effPing ? ' muted' : (effPing > 800 ? ' warn' : ''));
          valPing.textContent = effPing ? (effPing + 'ms') : '--';
          boxPing.appendChild(lblPing);
          boxPing.appendChild(valPing);

          pair.appendChild(boxChat);
          pair.appendChild(boxPing);
          card.appendChild(pair);

          var upHead = document.createElement('div');
          upHead.className = 'uptime-head';
          var upLbl = document.createElement('span');
          upLbl.className = 'uptime-label';
          upLbl.textContent = '可用性, 7天';
          var upPct = document.createElement('span');
          upPct.className = 'uptime-pct';
          var pctNum = (m.uptime_7d_pct !== undefined && m.uptime_7d_pct !== null) ? Number(m.uptime_7d_pct) : 100;
          upPct.textContent = pctNum.toFixed(2) + '%';
          if (pctNum < 95) upPct.style.color = '#fbbf24';
          upHead.appendChild(upLbl);
          upHead.appendChild(upPct);
          card.appendChild(upHead);

          var upSub = document.createElement('div');
          upSub.className = 'uptime-sub';
          var recSpan = document.createElement('span');
          recSpan.textContent = '近 ' + (data.history_bar_count || 60) + ' 次记录';
          var cdSpan = document.createElement('span');
          cdSpan.className = 'card-countdown';
          cdSpan.textContent = remainSec + 's 后刷新';
          upSub.appendChild(recSpan);
          upSub.appendChild(cdSpan);
          card.appendChild(upSub);

          var bars = document.createElement('div');
          bars.className = 'bars-row';
          var ticks = Array.isArray(m.ticks) ? m.ticks : [];
          ticks.forEach(function (tk) {
            var b = document.createElement('div');
            var st = tk.state || 'empty';
            b.className = 'tick-bar ' + st;
            var tip = st === 'empty'
              ? '等待后续采样记录'
              : (fmtClock(tk.ts) + ' · ' + stateLabel(st) + (tk.chat_ms ? (' · 对话 ' + tk.chat_ms + 'ms') : ''));
            b.addEventListener('mouseenter', function (ev) { showTooltip(ev, tip); });
            b.addEventListener('mousemove', function (ev) { showTooltip(ev, tip); });
            b.addEventListener('mouseleave', hideTooltip);
            bars.appendChild(b);
          });
          card.appendChild(bars);

          var foot = document.createElement('div');
          foot.className = 'bars-foot';
          var pSpan = document.createElement('span');
          pSpan.textContent = 'PAST';
          var nSpan = document.createElement('span');
          nSpan.textContent = 'NOW';
          foot.appendChild(pSpan);
          foot.appendChild(nSpan);
          card.appendChild(foot);

          grid.appendChild(card);
        });
      }

      async function loadStatus() {
        try {
          var pingPromise = measureLivePing();
          var resp = await fetch('/api/public/status?_=' + Date.now(), { cache: 'no-store' });
          var data = await resp.json();
          var pingMs = await pingPromise;

          refreshSec = Number(data.refresh_interval_sec || 45);
          remainSec = refreshSec;

          var pill = document.getElementById('overallPill');
          var pillTxt = document.getElementById('overallText');
          var tState = data.tunnel_state || (!data.online ? 'down' : (data.busy ? 'busy' : 'up'));
          var pillCls = (!data.online || tState === 'down') ? 'down' : ((data.busy || tState === 'local_only' || tState === 'warming') ? 'busy' : 'up');
          pill.className = 'status-pill ' + pillCls;
          pillTxt.textContent = data.overall_label || (data.online ? '服务运行正常' : '当前离线');

          var origin = (data.public_origin && data.public_origin.indexOf('http') === 0)
            ? data.public_origin
            : window.location.origin;
          latestProbeUrl = origin.replace(/\/+$/, '') + '/status/ping';
          latestApiBase = origin.replace(/\/+$/, '') + '/v1';

          document.getElementById('footerApiBase').textContent = data.is_public_origin
            ? latestApiBase
            : (latestApiBase + ' （当前仅本机可用，开启公网通道后自动生成外部 HTTPS 地址）');

          var chinazBtn = document.getElementById('openChinazBtn');
          var boceBtn = document.getElementById('openBoceBtn');
          if (chinazBtn && data.chinaz_url) chinazBtn.href = data.chinaz_url;
          if (boceBtn && data.boce_url) boceBtn.href = data.boce_url;
          if (data.network_map && data.network_map.ok) {
            renderNetworkMap(data.network_map);
          }

          renderCards(data, pingMs);
        } catch (_) {
          var pill = document.getElementById('overallPill');
          pill.className = 'status-pill down';
          document.getElementById('overallText').textContent = '连接中断';
        }
      }

      function copyWithFeedback(btn, text) {
        if (!text) return;
        var orig = btn.textContent;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            btn.textContent = '已复制 ✓';
            setTimeout(function () { btn.textContent = orig; }, 1400);
          }).catch(function () {});
        }
      }

      document.getElementById('manualRefreshBtn').addEventListener('click', function () {
        loadStatus();
      });
      document.getElementById('runMapProbeBtn').addEventListener('click', function () {
        triggerLiveNetworkMap(false);
      });
      document.getElementById('openItdogBtn').addEventListener('click', function () {
        copyWithFeedback(document.getElementById('openItdogBtn'), latestProbeUrl);
        window.open('https://www.itdog.cn/http/', '_blank', 'noopener,noreferrer');
      });
      document.getElementById('copyProbeBtn').addEventListener('click', function () {
        copyWithFeedback(document.getElementById('copyProbeBtn'), latestProbeUrl);
      });
      document.getElementById('copyApiBaseBtn').addEventListener('click', function () {
        copyWithFeedback(document.getElementById('copyApiBaseBtn'), latestApiBase);
      });

      setInterval(function () {
        remainSec -= 1;
        if (remainSec <= 0) {
          remainSec = refreshSec;
          loadStatus();
        }
        var topCd = document.getElementById('topCountdown');
        if (topCd) topCd.textContent = String(remainSec);
        var cardCds = document.querySelectorAll('.card-countdown');
        for (var i = 0; i < cardCds.length; i += 1) {
          cardCds[i].textContent = remainSec + 's 后刷新';
        }
      }, 1000);

      loadStatus();
      if (window.location.hash === '#network-map') {
        setTimeout(function () { triggerLiveNetworkMap(false); }, 300);
      }
    })();
  </script>
</body>
</html>
"""

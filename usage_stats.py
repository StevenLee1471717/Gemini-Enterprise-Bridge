"""Official Google Gemini API Pricing Engine, Token Ledger & Cost Equivalence Tracker.

Rates reflect Google Gemini Developer API Paid Tier Standard pricing (USD per 1M tokens,
retrieved from https://ai.google.dev/gemini-api/docs/pricing as of November 2026):

- gemini-4-carbon / gemini-4-argon (1,048,576 context):
    * Introductory Standard (through 2026-11-30): Input $2.00 | Cached $0.10 | Output $10.00
    * Post-intro Standard   (from 2026-12-01):    Input $4.00 | Cached $0.20 | Output $20.00
- gemini-3.1-pro (1,048,576 context, tiered at 200k prompt tokens):
    * <= 200k prompt tokens: Input $2.00 | Cached $0.20 | Output $12.00
    * >  200k prompt tokens: Input $4.00 | Cached $0.40 | Output $18.00
- gemini-3.8-flash (1,048,576 context):
    * Introductory Standard (through 2026-12-31): Input $0.75 | Cached $0.075 | Output $3.75
    * Post-intro Standard   (from 2027-01-01):    Input $1.50 | Cached $0.15  | Output $7.50
- gemini-2.5-pro (1,048,576 context, tiered at 200k prompt tokens):
    * <= 200k prompt tokens: Input $1.25 | Cached $0.125 | Output $10.00
    * >  200k prompt tokens: Input $2.50 | Cached $0.25  | Output $15.00
- gemini-2.5-flash (1,048,576 context):
    * Standard: Input $0.30 | Cached $0.03 | Output $2.50
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from schemas import resolve_target_web_model

logger = logging.getLogger("gemini_bridge.usage")

DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_USD_TO_CNY = 6.8
MAX_RECENT_REQUESTS = 20
MAX_STORED_DAYS = 90


@dataclass(frozen=True)
class ModelPricingRate:
    canonical_model: str
    display_name: str
    context_window: int
    input_per_1m: float
    cached_input_per_1m: float
    output_per_1m: float
    rate_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_model": self.canonical_model,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "input_rate_per_1m": self.input_per_1m,
            "cached_input_rate_per_1m": self.cached_input_per_1m,
            "output_rate_per_1m": self.output_per_1m,
            "rate_note": self.rate_note,
        }


_WEB_KEY_TO_CANONICAL: Dict[str, str] = {
    "4-carbon": "gemini-4-carbon",
    "4-argon": "gemini-4-argon",
    "3.1-pro": "gemini-3.1-pro",
    "3.8-flash": "gemini-3.8-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.5-flash": "gemini-2.5-flash",
}


def _match_browser_label_to_canonical(raw_label: Optional[str]) -> Optional[str]:
    if not raw_label:
        return None
    cleaned = raw_label.strip()
    if not cleaned:
        return None
    via_alias = resolve_target_web_model(cleaned)
    if via_alias in _WEB_KEY_TO_CANONICAL:
        return _WEB_KEY_TO_CANONICAL[via_alias]
    low = cleaned.lower()
    if "carbon" in low:
        return "gemini-4-carbon"
    if "argon" in low:
        return "gemini-4-argon"
    if "3.1" in low:
        return "gemini-3.1-pro"
    if "3.8" in low:
        return "gemini-3.8-flash"
    if "2.5" in low and "pro" in low:
        return "gemini-2.5-pro"
    if "2.5" in low and "flash" in low:
        return "gemini-2.5-flash"
    return None


def resolve_canonical_billing_model(
    requested_model: Optional[str],
    browser_active_model: Optional[str] = None,
    gcloud_fallback_model: Optional[str] = None,
) -> str:
    """Resolve requested OpenAI model + live browser active_model into canonical billing ID."""
    req_target = resolve_target_web_model(requested_model)
    if req_target in _WEB_KEY_TO_CANONICAL:
        return _WEB_KEY_TO_CANONICAL[req_target]

    from_browser = _match_browser_label_to_canonical(browser_active_model)
    if from_browser:
        return from_browser

    if requested_model and "gcloud" in requested_model.lower():
        from_gcloud = _match_browser_label_to_canonical(gcloud_fallback_model)
        if from_gcloud:
            return from_gcloud
        return "gemini-2.5-pro"

    from_req_fuzzy = _match_browser_label_to_canonical(requested_model)
    if from_req_fuzzy:
        return from_req_fuzzy

    return "gemini-enterprise-auto"


def _coerce_date(val: Optional[Union[date, datetime, str]]) -> date:
    if val is None:
        return datetime.now().date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        s = val.strip()[:10]
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
    return datetime.now().date()


def get_context_window_for_model(
    model_name: Optional[str],
    browser_active_model: Optional[str] = None,
) -> int:
    _ = resolve_canonical_billing_model(model_name, browser_active_model)
    return DEFAULT_CONTEXT_WINDOW


def get_model_pricing(
    canonical_model: str,
    prompt_tokens: int = 0,
    as_of_date: Optional[Union[date, datetime, str]] = None,
) -> ModelPricingRate:
    """Return official Google Gemini Developer API Paid Tier Standard rate (USD / 1M tokens)."""
    d = _coerce_date(as_of_date)
    key = (canonical_model or "").strip().lower()
    if key in _WEB_KEY_TO_CANONICAL:
        key = _WEB_KEY_TO_CANONICAL[key]

    if key == "gemini-4-carbon":
        if d <= date(2026, 11, 30):
            return ModelPricingRate(
                canonical_model="gemini-4-carbon",
                display_name="Gemini 4 Carbon",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=2.00,
                cached_input_per_1m=0.10,
                output_per_1m=10.00,
                rate_note="官方推广期标准价 (至 2026-11-30; 12月起 $4/$20)",
            )
        return ModelPricingRate(
            canonical_model="gemini-4-carbon",
            display_name="Gemini 4 Carbon",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=4.00,
            cached_input_per_1m=0.20,
            output_per_1m=20.00,
            rate_note="官方正式标准价",
        )

    if key == "gemini-4-argon":
        if d <= date(2026, 11, 30):
            return ModelPricingRate(
                canonical_model="gemini-4-argon",
                display_name="Gemini 4 Argon",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=2.00,
                cached_input_per_1m=0.10,
                output_per_1m=10.00,
                rate_note="官方推广期标准价 (至 2026-11-30; 12月起 $4/$20)",
            )
        return ModelPricingRate(
            canonical_model="gemini-4-argon",
            display_name="Gemini 4 Argon",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=4.00,
            cached_input_per_1m=0.20,
            output_per_1m=20.00,
            rate_note="官方正式标准价",
        )

    if key == "gemini-3.1-pro":
        if prompt_tokens > 200_000:
            return ModelPricingRate(
                canonical_model="gemini-3.1-pro",
                display_name="Gemini 3.1 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=4.00,
                cached_input_per_1m=0.40,
                output_per_1m=18.00,
                rate_note="官方 >200K 长上下文阶梯价",
            )
        return ModelPricingRate(
            canonical_model="gemini-3.1-pro",
            display_name="Gemini 3.1 Pro",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.20,
            output_per_1m=12.00,
            rate_note="官方 <=200K 标准价 (>200K 为 $4/$18)",
        )

    if key == "gemini-3.8-flash":
        if d <= date(2026, 12, 31):
            return ModelPricingRate(
                canonical_model="gemini-3.8-flash",
                display_name="Gemini 3.8 Flash",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=0.75,
                cached_input_per_1m=0.075,
                output_per_1m=3.75,
                rate_note="官方推广期标准价 (至 2026-12-31; 2027起 $1.50/$7.50)",
            )
        return ModelPricingRate(
            canonical_model="gemini-3.8-flash",
            display_name="Gemini 3.8 Flash",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=1.50,
            cached_input_per_1m=0.15,
            output_per_1m=7.50,
            rate_note="官方正式标准价",
        )

    if key == "gemini-2.5-pro":
        if prompt_tokens > 200_000:
            return ModelPricingRate(
                canonical_model="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=2.50,
                cached_input_per_1m=0.25,
                output_per_1m=15.00,
                rate_note="官方 >200K 长上下文阶梯价",
            )
        return ModelPricingRate(
            canonical_model="gemini-2.5-pro",
            display_name="Gemini 2.5 Pro",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=1.25,
            cached_input_per_1m=0.125,
            output_per_1m=10.00,
            rate_note="官方 <=200K 标准价 (>200K 为 $2.50/$15)",
        )

    if key == "gemini-2.5-flash":
        return ModelPricingRate(
            canonical_model="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=0.30,
            cached_input_per_1m=0.03,
            output_per_1m=2.50,
            rate_note="官方标准价",
        )

    # Frontier default when web UI is kept on "Auto" and actual concrete model isn't exposed
    if d <= date(2026, 11, 30):
        return ModelPricingRate(
            canonical_model="gemini-enterprise-auto",
            display_name="Auto (按 Frontier 默认折算)",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.10,
            output_per_1m=10.00,
            rate_note="网页 Auto 模式默认参考 Gemini 4 Frontier 单价",
        )
    return ModelPricingRate(
        canonical_model="gemini-enterprise-auto",
        display_name="Auto (按 Frontier 默认折算)",
        context_window=DEFAULT_CONTEXT_WINDOW,
        input_per_1m=2.00,
        cached_input_per_1m=0.20,
        output_per_1m=12.00,
        rate_note="网页 Auto 模式默认参考 Gemini Pro Frontier 单价",
    )


def list_official_pricing_catalog(
    as_of_date: Optional[Union[date, datetime, str]] = None,
) -> List[Dict[str, Any]]:
    """Return full pricing catalog for UI display."""
    ordered = [
        "gemini-4-carbon",
        "gemini-4-argon",
        "gemini-3.1-pro",
        "gemini-3.8-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-enterprise-auto",
    ]
    return [get_model_pricing(m, prompt_tokens=0, as_of_date=as_of_date).to_dict() for m in ordered]


def _empty_bucket() -> Dict[str, Any]:
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "delta_prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "equivalent_cost_usd": 0.0,
        "cached_equivalent_cost_usd": 0.0,
    }


def _enrich_bucket(bucket: Dict[str, Any], usd_to_cny: float) -> Dict[str, Any]:
    uncached_usd = float(bucket.get("equivalent_cost_usd") or 0.0)
    cached_usd = float(bucket.get("cached_equivalent_cost_usd") or 0.0)
    return {
        "requests": int(bucket.get("requests") or 0),
        "prompt_tokens": int(bucket.get("prompt_tokens") or 0),
        "cached_tokens": int(bucket.get("cached_tokens") or 0),
        "delta_prompt_tokens": int(bucket.get("delta_prompt_tokens") or 0),
        "completion_tokens": int(bucket.get("completion_tokens") or 0),
        "total_tokens": int(bucket.get("total_tokens") or 0),
        "equivalent_cost_usd": round(uncached_usd, 6),
        "cached_equivalent_cost_usd": round(cached_usd, 6),
        "equivalent_cost_cny": round(uncached_usd * usd_to_cny, 4),
        "cached_equivalent_cost_cny": round(cached_usd * usd_to_cny, 4),
    }


def _accumulate_bucket(
    bucket: Dict[str, Any],
    prompt_tokens: int,
    cached_tokens: int,
    delta_prompt_tokens: int,
    completion_tokens: int,
    uncached_usd: float,
    cached_usd: float,
) -> None:
    bucket["requests"] = int(bucket.get("requests") or 0) + 1
    bucket["prompt_tokens"] = int(bucket.get("prompt_tokens") or 0) + prompt_tokens
    bucket["cached_tokens"] = int(bucket.get("cached_tokens") or 0) + cached_tokens
    bucket["delta_prompt_tokens"] = int(bucket.get("delta_prompt_tokens") or 0) + delta_prompt_tokens
    bucket["completion_tokens"] = int(bucket.get("completion_tokens") or 0) + completion_tokens
    bucket["total_tokens"] = int(bucket.get("total_tokens") or 0) + prompt_tokens + completion_tokens
    bucket["equivalent_cost_usd"] = round(
        float(bucket.get("equivalent_cost_usd") or 0.0) + uncached_usd, 8
    )
    bucket["cached_equivalent_cost_usd"] = round(
        float(bucket.get("cached_equivalent_cost_usd") or 0.0) + cached_usd, 8
    )


class UsageTracker:
    """Thread-safe persistent token usage & official Gemini API cost equivalence ledger."""

    def __init__(
        self,
        storage_path: Optional[Union[Path, str]] = None,
        usd_to_cny_rate: float = DEFAULT_USD_TO_CNY,
    ) -> None:
        if storage_path:
            self.storage_path = Path(storage_path)
        else:
            self.storage_path = Path(__file__).resolve().parent / "usage_stats.json"
        self.usd_to_cny_rate = usd_to_cny_rate if usd_to_cny_rate > 0 else DEFAULT_USD_TO_CNY
        self._lock = threading.RLock()
        self._started_at = int(time.time())
        self._all_time: Dict[str, Any] = _empty_bucket()
        self._by_day: Dict[str, Dict[str, Any]] = {}
        self._current_session: Dict[str, Any] = _empty_bucket()
        self._by_model: Dict[str, Dict[str, Any]] = {}
        self._recent_requests: List[Dict[str, Any]] = []
        self._load_from_disk()

    @staticmethod
    def _local_day_key(as_of_date: Optional[Union[date, datetime, str]] = None) -> str:
        return _coerce_date(as_of_date).isoformat()

    def _load_from_disk(self) -> None:
        if not self.storage_path.is_file():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            if isinstance(raw.get("all_time"), dict):
                self._all_time.update(raw["all_time"])
            if isinstance(raw.get("by_day"), dict):
                for k, v in raw["by_day"].items():
                    if isinstance(v, dict):
                        b = _empty_bucket()
                        b.update(v)
                        self._by_day[str(k)] = b
            if isinstance(raw.get("by_model"), dict):
                for k, v in raw["by_model"].items():
                    if isinstance(v, dict):
                        b = _empty_bucket()
                        b.update(v)
                        self._by_model[str(k)] = b
            if isinstance(raw.get("recent_requests"), list):
                self._recent_requests = [
                    x for x in raw["recent_requests"][-MAX_RECENT_REQUESTS:] if isinstance(x, dict)
                ]
        except Exception as exc:
            logger.warning("加载用量统计文件 %s 失败（将重新初始化）: %s", self.storage_path, exc)

    def _save_to_disk_unlocked(self) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._by_day) > MAX_STORED_DAYS:
                keep_keys = sorted(self._by_day.keys())[-MAX_STORED_DAYS:]
                self._by_day = {k: self._by_day[k] for k in keep_keys}
            payload = {
                "version": 1,
                "updated_at": int(time.time()),
                "all_time": self._all_time,
                "by_day": self._by_day,
                "by_model": self._by_model,
                "recent_requests": self._recent_requests[-MAX_RECENT_REQUESTS:],
            }
            tmp_path = self.storage_path.with_name(self.storage_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.storage_path)
        except Exception as exc:
            logger.warning("保存用量统计文件 %s 失败: %s", self.storage_path, exc)

    def record_request(
        self,
        requested_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        browser_active_model: Optional[str] = None,
        gcloud_fallback_model: Optional[str] = None,
        strategy: str = "smart_single_turn",
        cached_tokens: int = 0,
        delta_prompt_tokens: Optional[int] = None,
        slot_key: str = "",
        tab_id: Optional[int] = None,
        context_tokens: Optional[int] = None,
        context_limit: Optional[int] = None,
        duration_ms: int = 0,
        as_of_date: Optional[Union[date, datetime, str]] = None,
    ) -> Dict[str, Any]:
        """Record one completed API request and return its computed cost & context record."""
        p_tokens = max(0, int(prompt_tokens or 0))
        c_tokens = max(0, int(completion_tokens or 0))

        if strategy == "smart_incremental_delta" and cached_tokens > 0:
            eff_cached = min(p_tokens, max(0, int(cached_tokens)))
            if delta_prompt_tokens is not None:
                eff_delta = max(0, int(delta_prompt_tokens))
                if eff_cached + eff_delta > p_tokens:
                    eff_cached = max(0, p_tokens - eff_delta)
                else:
                    eff_delta = max(0, p_tokens - eff_cached)
            else:
                eff_delta = max(0, p_tokens - eff_cached)
        else:
            eff_cached = 0
            eff_delta = p_tokens

        canonical_model = resolve_canonical_billing_model(
            requested_model=requested_model,
            browser_active_model=browser_active_model,
            gcloud_fallback_model=gcloud_fallback_model,
        )
        rate = get_model_pricing(
            canonical_model=canonical_model,
            prompt_tokens=p_tokens,
            as_of_date=as_of_date,
        )

        uncached_usd = (
            (p_tokens / 1_000_000.0) * rate.input_per_1m
            + (c_tokens / 1_000_000.0) * rate.output_per_1m
        )
        cached_usd = (
            (eff_cached / 1_000_000.0) * rate.cached_input_per_1m
            + (eff_delta / 1_000_000.0) * rate.input_per_1m
            + (c_tokens / 1_000_000.0) * rate.output_per_1m
        )

        ctx_limit = int(context_limit or rate.context_window or DEFAULT_CONTEXT_WINDOW)
        ctx_tokens = int(context_tokens if context_tokens is not None else (p_tokens + c_tokens))
        util_pct = round(min(100.0, (ctx_tokens / max(ctx_limit, 1)) * 100.0), 2)
        day_key = self._local_day_key(as_of_date)
        now_ts = int(time.time())

        record = {
            "ts": now_ts,
            "slot_key": slot_key,
            "tab_id": tab_id,
            "requested_model": requested_model,
            "model": canonical_model,
            "display_name": rate.display_name,
            "strategy": strategy,
            "prompt_tokens": p_tokens,
            "cached_tokens": eff_cached,
            "delta_prompt_tokens": eff_delta,
            "completion_tokens": c_tokens,
            "total_tokens": p_tokens + c_tokens,
            "context_tokens": ctx_tokens,
            "context_limit": ctx_limit,
            "context_utilization_pct": util_pct,
            "equivalent_cost_usd": round(uncached_usd, 6),
            "cached_equivalent_cost_usd": round(cached_usd, 6),
            "equivalent_cost_cny": round(uncached_usd * self.usd_to_cny_rate, 4),
            "cached_equivalent_cost_cny": round(cached_usd * self.usd_to_cny_rate, 4),
            "input_rate_per_1m": rate.input_per_1m,
            "cached_input_rate_per_1m": rate.cached_input_per_1m,
            "output_rate_per_1m": rate.output_per_1m,
            "duration_ms": max(0, int(duration_ms or 0)),
        }

        with self._lock:
            _accumulate_bucket(
                self._all_time,
                p_tokens,
                eff_cached,
                eff_delta,
                c_tokens,
                uncached_usd,
                cached_usd,
            )
            day_bucket = self._by_day.get(day_key)
            if day_bucket is None:
                day_bucket = _empty_bucket()
                self._by_day[day_key] = day_bucket
            _accumulate_bucket(
                day_bucket,
                p_tokens,
                eff_cached,
                eff_delta,
                c_tokens,
                uncached_usd,
                cached_usd,
            )
            _accumulate_bucket(
                self._current_session,
                p_tokens,
                eff_cached,
                eff_delta,
                c_tokens,
                uncached_usd,
                cached_usd,
            )
            model_bucket = self._by_model.get(canonical_model)
            if model_bucket is None:
                model_bucket = _empty_bucket()
                self._by_model[canonical_model] = model_bucket
            _accumulate_bucket(
                model_bucket,
                p_tokens,
                eff_cached,
                eff_delta,
                c_tokens,
                uncached_usd,
                cached_usd,
            )
            model_bucket["display_name"] = rate.display_name
            model_bucket["input_rate_per_1m"] = rate.input_per_1m
            model_bucket["cached_input_rate_per_1m"] = rate.cached_input_per_1m
            model_bucket["output_rate_per_1m"] = rate.output_per_1m

            self._recent_requests.append(record)
            if len(self._recent_requests) > MAX_RECENT_REQUESTS:
                self._recent_requests = self._recent_requests[-MAX_RECENT_REQUESTS:]
            self._save_to_disk_unlocked()

        return record

    def reset(self, scope: str = "all", as_of_date: Optional[Union[date, datetime, str]] = None) -> Dict[str, Any]:
        """Reset usage counters for `session`, `today`, or `all`."""
        norm_scope = (scope or "all").strip().lower()
        day_key = self._local_day_key(as_of_date)
        with self._lock:
            if norm_scope == "session":
                self._current_session = _empty_bucket()
            elif norm_scope == "today":
                self._by_day.pop(day_key, None)
                self._current_session = _empty_bucket()
                self._save_to_disk_unlocked()
            else:
                self._all_time = _empty_bucket()
                self._by_day.clear()
                self._current_session = _empty_bucket()
                self._by_model.clear()
                self._recent_requests.clear()
                self._save_to_disk_unlocked()
        return self.snapshot(as_of_date=as_of_date)

    def snapshot(
        self,
        active_model_hint: Optional[str] = None,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        include_catalog: bool = False,
    ) -> Dict[str, Any]:
        """Return JSON-serializable usage & cost equivalence snapshot."""
        day_key = self._local_day_key(as_of_date)
        canonical_active = resolve_canonical_billing_model(None, active_model_hint)
        active_rate = get_model_pricing(canonical_active, prompt_tokens=0, as_of_date=as_of_date)

        with self._lock:
            today_raw = self._by_day.get(day_key) or _empty_bucket()
            by_model_out: Dict[str, Dict[str, Any]] = {}
            sorted_models = sorted(
                self._by_model.items(),
                key=lambda kv: float(kv[1].get("equivalent_cost_usd") or 0.0),
                reverse=True,
            )
            for m_key, m_bucket in sorted_models:
                m_rate = get_model_pricing(m_key, prompt_tokens=0, as_of_date=as_of_date)
                enriched = _enrich_bucket(m_bucket, self.usd_to_cny_rate)
                enriched["display_name"] = m_bucket.get("display_name") or m_rate.display_name
                enriched["input_rate_per_1m"] = m_rate.input_per_1m
                enriched["cached_input_rate_per_1m"] = m_rate.cached_input_per_1m
                enriched["output_rate_per_1m"] = m_rate.output_per_1m
                enriched["rate_note"] = m_rate.rate_note
                by_model_out[m_key] = enriched

            result: Dict[str, Any] = {
                "today_date": day_key,
                "started_at": self._started_at,
                "usd_to_cny_rate": self.usd_to_cny_rate,
                "active_pricing": active_rate.to_dict(),
                "all_time": _enrich_bucket(self._all_time, self.usd_to_cny_rate),
                "today": _enrich_bucket(today_raw, self.usd_to_cny_rate),
                "current_session": _enrich_bucket(self._current_session, self.usd_to_cny_rate),
                "by_model": by_model_out,
                "recent_requests": list(reversed(self._recent_requests[-MAX_RECENT_REQUESTS:])),
            }
            if include_catalog:
                result["pricing_catalog"] = list_official_pricing_catalog(as_of_date=as_of_date)
            return result

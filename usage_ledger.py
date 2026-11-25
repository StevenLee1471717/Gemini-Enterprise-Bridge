"""Independent local token, multimodal & official API cost-equivalence ledger.

Design guarantees:
- Single source of truth by day -> (by_model, by_task) plus an optional archive bucket,
  so resetting `today` or `session` never causes drift or double-counting in `all_time`
  or `by_model`.
- Uses the local `schemas.estimate_tokens` ruler without altering compaction thresholds.
- Covers chat, Codex/Responses, conversation compaction, image gen/edit, video gen, and audio.
- Configurable exchange rate and per-model/media reference rates (no date time-bombs).
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from platform_compat import (
    BILLING_TIMEZONE,
    atomic_write_json,
    billing_day_key,
    billing_window_info,
    resolve_timezone,
)
from schemas import (
    format_gemini_display_name,
    normalize_web_label_to_model_spec,
    resolve_target_web_model,
)

logger = logging.getLogger("gemini_bridge.ledger")

DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_COMPACT_BUDGET = 300_000
DEFAULT_USD_TO_CNY = 6.8
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_BILLING_TIMEZONE = BILLING_TIMEZONE
# Empirical + Google Cloud Gemini Enterprise EDU pooled quota defaults:
# - Upstream metric: discoveryengine.googleapis.com/text_answer_gen_tier_edu_regional
# - Resets daily at 00:00 America/Los_Angeles (16:00 PST / 15:00 PDT in Asia/Shanghai)
# - EDU edition grants 40 Assistant queries / seat / day; ~50 cohort seats => 2,000 QPD shared pool
#   (verified against observed bridge ceiling: 1,566 OK on 11-23; 1,555 + classmate usage hit quota on 11-24)
# - Default classmate reserve: 600 QPD (30% of pool, ~15 full EDU seats), leaving 1,400 QPD safe budget.
DEFAULT_COHORT_DAILY_QUOTA = 2000
DEFAULT_COHORT_RESERVED_QUOTA = 600
DEFAULT_IMAGE_UNIT_USD = 0.04
DEFAULT_VIDEO_UNIT_USD = 0.35
MAX_RECENT_REQUESTS = 300
MAX_STORED_DAYS = 180
LEDGER_SCHEMA_VERSION = 2

TASK_LABELS: Dict[str, str] = {
    "chat": "普通对话",
    "responses": "Codex / Responses",
    "compaction": "长对话浓缩",
    "image_gen": "图片生成",
    "image_edit": "图片修改",
    "video_gen": "视频生成",
    "audio": "语音转写/翻译",
}


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
    "4-hydrogen": "gemini-4-hydrogen",
    "3.1-pro": "gemini-3.1-pro",
    "3.8-flash": "gemini-3.8-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.5-flash": "gemini-2.5-flash",
}


def _match_label_to_canonical(raw_label: Optional[str]) -> Optional[str]:
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
    if "hydrogen" in low:
        return "gemini-4-hydrogen"
    if "3.1" in low:
        return "gemini-3.1-pro"
    if "3.8" in low:
        return "gemini-3.8-flash"
    if "2.5" in low and "pro" in low:
        return "gemini-2.5-pro"
    if "2.5" in low and "flash" in low:
        return "gemini-2.5-flash"
    if via_alias and via_alias != "auto":
        spec = normalize_web_label_to_model_spec(cleaned)
        if spec and spec[0] not in {"gemini-enterprise-web", "gemini-enterprise-gcloud"}:
            return spec[0]
    return None


def resolve_canonical_billing_model(
    requested_model: Optional[str],
    browser_active_model: Optional[str] = None,
    gcloud_fallback_model: Optional[str] = None,
) -> str:
    """Resolve requested OpenAI model + live browser model into a canonical billing key."""
    req_target = resolve_target_web_model(requested_model)
    if req_target in _WEB_KEY_TO_CANONICAL:
        return _WEB_KEY_TO_CANONICAL[req_target]
    if req_target and req_target != "auto":
        from_req_dynamic = _match_label_to_canonical(requested_model)
        if from_req_dynamic:
            return from_req_dynamic

    from_browser = _match_label_to_canonical(browser_active_model)
    if from_browser:
        return from_browser

    if requested_model and "gcloud" in requested_model.lower():
        from_gcloud = _match_label_to_canonical(gcloud_fallback_model)
        return from_gcloud or "gemini-2.5-pro"

    from_fuzzy = _match_label_to_canonical(requested_model)
    if from_fuzzy:
        return from_fuzzy

    return "gemini-4-carbon"


def get_model_pricing(
    canonical_model: str,
    prompt_tokens: int = 0,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ModelPricingRate:
    """Return reference Google Gemini API pricing (USD per 1M tokens) with optional overrides."""
    raw_model = (canonical_model or "").strip()
    key = raw_model.lower()
    if key in _WEB_KEY_TO_CANONICAL:
        key = _WEB_KEY_TO_CANONICAL[key]

    if key == "gemini-4-carbon":
        base = ModelPricingRate(
            canonical_model="gemini-4-carbon",
            display_name="Gemini 4 Carbon",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.10,
            output_per_1m=10.00,
            rate_note="参考单价 ($2.00 / $0.10 / $10.00 per 1M)",
        )
    elif key == "gemini-4-argon":
        base = ModelPricingRate(
            canonical_model="gemini-4-argon",
            display_name="Gemini 4 Argon",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.10,
            output_per_1m=10.00,
            rate_note="参考单价 ($2.00 / $0.10 / $10.00 per 1M)",
        )
    elif key == "gemini-4-hydrogen":
        base = ModelPricingRate(
            canonical_model="gemini-4-hydrogen",
            display_name="Gemini 4 Hydrogen",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.10,
            output_per_1m=10.00,
            rate_note="参考单价 ($2.00 / $0.10 / $10.00 per 1M)",
        )
    elif key == "gemini-3.1-pro":
        if prompt_tokens > 200_000:
            base = ModelPricingRate(
                canonical_model="gemini-3.1-pro",
                display_name="Gemini 3.1 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=4.00,
                cached_input_per_1m=0.40,
                output_per_1m=18.00,
                rate_note=">200K 长上下文阶梯价 ($4 / $0.40 / $18)",
            )
        else:
            base = ModelPricingRate(
                canonical_model="gemini-3.1-pro",
                display_name="Gemini 3.1 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=2.00,
                cached_input_per_1m=0.20,
                output_per_1m=12.00,
                rate_note="<=200K 标准价 (>200K 为 $4/$18)",
            )
    elif key == "gemini-3.8-flash":
        base = ModelPricingRate(
            canonical_model="gemini-3.8-flash",
            display_name="Gemini 3.8 Flash",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=0.75,
            cached_input_per_1m=0.075,
            output_per_1m=3.75,
            rate_note="参考单价 ($0.75 / $0.075 / $3.75 per 1M)",
        )
    elif key == "gemini-2.5-pro":
        if prompt_tokens > 200_000:
            base = ModelPricingRate(
                canonical_model="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=2.50,
                cached_input_per_1m=0.25,
                output_per_1m=15.00,
                rate_note=">200K 长上下文阶梯价 ($2.50 / $0.25 / $15)",
            )
        else:
            base = ModelPricingRate(
                canonical_model="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro",
                context_window=DEFAULT_CONTEXT_WINDOW,
                input_per_1m=1.25,
                cached_input_per_1m=0.125,
                output_per_1m=10.00,
                rate_note="<=200K 标准价 (>200K 为 $2.50/$15)",
            )
    elif key == "gemini-2.5-flash":
        base = ModelPricingRate(
            canonical_model="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=0.30,
            cached_input_per_1m=0.03,
            output_per_1m=2.50,
            rate_note="官方标准价 ($0.30 / $0.03 / $2.50 per 1M)",
        )
    elif key and key not in {"auto", "default", "gemini-enterprise-auto", "gemini-enterprise-web"}:
        canonical_id = raw_model if key.startswith("gemini-web:") else key
        disp = format_gemini_display_name(raw_model)
        is_light = any(t in key for t in ("flash", "lite", "nano", "mini"))
        base = ModelPricingRate(
            canonical_model=canonical_id,
            display_name=disp,
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=0.75 if is_light else 2.00,
            cached_input_per_1m=0.075 if is_light else 0.10,
            output_per_1m=3.75 if is_light else 10.00,
            rate_note=(
                "自动适配轻量档参考单价 ($0.75 / $0.075 / $3.75 per 1M)"
                if is_light
                else "自动适配主力档参考单价 ($2.00 / $0.10 / $10.00 per 1M)"
            ),
        )
    else:
        base = ModelPricingRate(
            canonical_model="gemini-enterprise-auto",
            display_name="Auto (按 Frontier 默认折算)",
            context_window=DEFAULT_CONTEXT_WINDOW,
            input_per_1m=2.00,
            cached_input_per_1m=0.10,
            output_per_1m=10.00,
            rate_note="网页 Auto 模式默认参考 Frontier 单价",
        )

    if overrides and isinstance(overrides.get(base.canonical_model), dict):
        ov = overrides[base.canonical_model]
        return ModelPricingRate(
            canonical_model=base.canonical_model,
            display_name=str(ov.get("display_name") or base.display_name),
            context_window=int(ov.get("context_window") or base.context_window),
            input_per_1m=float(ov.get("input_per_1m", base.input_per_1m)),
            cached_input_per_1m=float(ov.get("cached_input_per_1m", base.cached_input_per_1m)),
            output_per_1m=float(ov.get("output_per_1m", base.output_per_1m)),
            rate_note=str(ov.get("rate_note") or "自定义覆盖单价"),
        )
    return base


def list_pricing_catalog(
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ordered = [
        "gemini-4-carbon",
        "gemini-4-hydrogen",
        "gemini-4-argon",
        "gemini-3.1-pro",
        "gemini-3.8-flash",
    ]
    if overrides:
        for extra_k in overrides:
            if extra_k not in ordered:
                ordered.append(extra_k)
    return [get_model_pricing(m, prompt_tokens=0, overrides=overrides).to_dict() for m in ordered]


def estimate_attachment_input_tokens(attachments: Optional[Sequence[Dict[str, Any]]]) -> int:
    """Estimate multimodal input token equivalent for ledger accounting only."""
    if not attachments:
        return 0
    total = 0
    for att in attachments:
        if not isinstance(att, dict):
            continue
        cat = str(att.get("category") or "").lower()
        size_b = int(att.get("size_bytes") or att.get("bytes") or att.get("size") or 0)
        if cat == "image":
            total += 258
        elif cat == "audio":
            total += min(4096, max(64, (size_b or 32_000) // 500))
        elif cat == "video":
            total += min(8192, max(258, (size_b or 128_000) // 2000))
        else:
            total += min(4096, max(128, (size_b or 16_000) // 1200))
    return total


_BUCKET_INT_KEYS = (
    "requests",
    "tool_calls",
    "prompt_tokens",
    "cached_tokens",
    "delta_prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "images_generated",
    "videos_generated",
    "audio_requests",
    "compactions",
)
_BUCKET_FLOAT_KEYS = (
    "equivalent_cost_usd",
    "cached_equivalent_cost_usd",
)


def empty_bucket() -> Dict[str, Any]:
    out: Dict[str, Any] = {k: 0 for k in _BUCKET_INT_KEYS}
    for fk in _BUCKET_FLOAT_KEYS:
        out[fk] = 0.0
    return out


def sanitize_bucket(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    b = empty_bucket()
    if not isinstance(raw, dict):
        return b
    for k in _BUCKET_INT_KEYS:
        try:
            b[k] = max(0, int(raw.get(k) or 0))
        except (TypeError, ValueError):
            b[k] = 0
    for fk in _BUCKET_FLOAT_KEYS:
        try:
            b[fk] = round(max(0.0, float(raw.get(fk) or 0.0)), 8)
        except (TypeError, ValueError):
            b[fk] = 0.0
    return b


def merge_bucket_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k in _BUCKET_INT_KEYS:
        dst[k] = int(dst.get(k) or 0) + int(src.get(k) or 0)
    for fk in _BUCKET_FLOAT_KEYS:
        dst[fk] = round(float(dst.get(fk) or 0.0) + float(src.get(fk) or 0.0), 8)


def empty_group() -> Dict[str, Any]:
    return {
        "summary": empty_bucket(),
        "by_model": {},
        "by_task": {},
        "by_tenant": {},
    }


def empty_tenant_group(tenant_name: str = "") -> Dict[str, Any]:
    return {
        "tenant_name": tenant_name,
        "errors": 0,
        "summary": empty_bucket(),
        "by_model": {},
        "by_task": {},
    }


def sanitize_tenant_group(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tg = empty_tenant_group()
    if not isinstance(raw, dict):
        return tg
    tg["tenant_name"] = str(raw.get("tenant_name") or "")
    try:
        tg["errors"] = max(0, int(raw.get("errors") or 0))
    except (TypeError, ValueError):
        tg["errors"] = 0
    tg["summary"] = sanitize_bucket(raw.get("summary"))
    if isinstance(raw.get("by_model"), dict):
        for mk, mv in raw["by_model"].items():
            if isinstance(mv, dict):
                tg["by_model"][str(mk)] = sanitize_bucket(mv)
    if isinstance(raw.get("by_task"), dict):
        for tk, tv in raw["by_task"].items():
            if isinstance(tv, dict):
                tg["by_task"][str(tk)] = sanitize_bucket(tv)
    return tg


def merge_tenant_group_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    if src.get("tenant_name"):
        dst["tenant_name"] = str(src["tenant_name"])
    dst["errors"] = int(dst.get("errors") or 0) + int(src.get("errors") or 0)
    merge_bucket_into(dst["summary"], src.get("summary") or empty_bucket())
    for mk, mb in (src.get("by_model") or {}).items():
        target_m = dst["by_model"].setdefault(mk, empty_bucket())
        merge_bucket_into(target_m, mb)
    for tk, tb in (src.get("by_task") or {}).items():
        target_t = dst["by_task"].setdefault(tk, empty_bucket())
        merge_bucket_into(target_t, tb)

def sanitize_group(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    g = empty_group()
    if not isinstance(raw, dict):
        return g
    g["summary"] = sanitize_bucket(raw.get("summary"))
    if isinstance(raw.get("by_model"), dict):
        for mk, mv in raw["by_model"].items():
            if isinstance(mv, dict):
                g["by_model"][str(mk)] = sanitize_bucket(mv)
    if isinstance(raw.get("by_task"), dict):
        for tk, tv in raw["by_task"].items():
            if isinstance(tv, dict):
                g["by_task"][str(tk)] = sanitize_bucket(tv)
    if isinstance(raw.get("by_tenant"), dict):
        for tid, tv in raw["by_tenant"].items():
            if isinstance(tv, dict):
                g["by_tenant"][str(tid)] = sanitize_tenant_group(tv)
    return g


def merge_group_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    merge_bucket_into(dst["summary"], src["summary"])
    for mk, mb in (src.get("by_model") or {}).items():
        target_m = dst["by_model"].setdefault(mk, empty_bucket())
        merge_bucket_into(target_m, mb)
    for tk, tb in (src.get("by_task") or {}).items():
        target_t = dst["by_task"].setdefault(tk, empty_bucket())
        merge_bucket_into(target_t, tb)
    dst_tenants = dst.setdefault("by_tenant", {})
    for tid, tgroup in (src.get("by_tenant") or {}).items():
        target_ten = dst_tenants.setdefault(tid, empty_tenant_group(str(tgroup.get("tenant_name") or "")))
        merge_tenant_group_into(target_ten, tgroup)


def _subtract_bucket(minuend: Dict[str, Any], subtrahend: Dict[str, Any]) -> Dict[str, Any]:
    out = empty_bucket()
    for k in _BUCKET_INT_KEYS:
        out[k] = max(0, int(minuend.get(k) or 0) - int(subtrahend.get(k) or 0))
    for fk in _BUCKET_FLOAT_KEYS:
        out[fk] = round(max(0.0, float(minuend.get(fk) or 0.0) - float(subtrahend.get(fk) or 0.0)), 8)
    return out


def reconcile_unattributed_host_tenant(day_group: Dict[str, Any]) -> bool:
    """Attribute pre-v1.5.0 unattributed daily usage remainder to the local `host` tenant."""
    if not isinstance(day_group, dict):
        return False
    summary = sanitize_bucket(day_group.get("summary"))
    if int(summary.get("requests") or 0) <= 0 and int(summary.get("total_tokens") or 0) <= 0:
        return False
    by_tenant = day_group.setdefault("by_tenant", {})
    summed_summary = empty_bucket()
    summed_models: Dict[str, Dict[str, Any]] = {}
    summed_tasks: Dict[str, Dict[str, Any]] = {}
    for tgroup in by_tenant.values():
        if not isinstance(tgroup, dict):
            continue
        merge_bucket_into(summed_summary, sanitize_bucket(tgroup.get("summary")))
        for mk, mb in (tgroup.get("by_model") or {}).items():
            if isinstance(mb, dict):
                merge_bucket_into(summed_models.setdefault(mk, empty_bucket()), sanitize_bucket(mb))
        for tk, tb in (tgroup.get("by_task") or {}).items():
            if isinstance(tb, dict):
                merge_bucket_into(summed_tasks.setdefault(tk, empty_bucket()), sanitize_bucket(tb))
    rem_summary = _subtract_bucket(summary, summed_summary)
    if int(rem_summary.get("requests") or 0) <= 0 and int(rem_summary.get("total_tokens") or 0) <= 0:
        return False
    host_group = sanitize_tenant_group(by_tenant.get("host")) if isinstance(by_tenant.get("host"), dict) else empty_tenant_group("本机直连")
    if not host_group.get("tenant_name"):
        host_group["tenant_name"] = "本机直连"
    by_tenant["host"] = host_group
    merge_bucket_into(host_group["summary"], rem_summary)
    for mk, mb in (day_group.get("by_model") or {}).items():
        if isinstance(mb, dict):
            rem_m = _subtract_bucket(sanitize_bucket(mb), summed_models.get(mk) or empty_bucket())
            if int(rem_m.get("requests") or 0) > 0 or int(rem_m.get("total_tokens") or 0) > 0:
                merge_bucket_into(host_group["by_model"].setdefault(mk, empty_bucket()), rem_m)
    for tk, tb in (day_group.get("by_task") or {}).items():
        if isinstance(tb, dict):
            rem_t = _subtract_bucket(sanitize_bucket(tb), summed_tasks.get(tk) or empty_bucket())
            if int(rem_t.get("requests") or 0) > 0 or int(rem_t.get("total_tokens") or 0) > 0:
                merge_bucket_into(host_group["by_task"].setdefault(tk, empty_bucket()), rem_t)
    return True


def enrich_bucket(bucket: Dict[str, Any], usd_to_cny: float) -> Dict[str, Any]:
    uncached_usd = float(bucket.get("equivalent_cost_usd") or 0.0)
    cached_usd = float(bucket.get("cached_equivalent_cost_usd") or 0.0)
    saved_usd = max(0.0, uncached_usd - cached_usd)
    reqs = int(bucket.get("requests") or 0)
    t_calls = int(bucket.get("tool_calls") or 0)
    p_tok = int(bucket.get("prompt_tokens") or 0)
    c_tok = int(bucket.get("completion_tokens") or 0)
    tot_tok = int(bucket.get("total_tokens") or 0)
    c_hit = int(bucket.get("cached_tokens") or 0)
    out: Dict[str, Any] = {k: int(bucket.get(k) or 0) for k in _BUCKET_INT_KEYS}
    out.update(
        {
            "equivalent_cost_usd": round(uncached_usd, 6),
            "cached_equivalent_cost_usd": round(cached_usd, 6),
            "saved_cost_usd": round(saved_usd, 6),
            "equivalent_cost_cny": round(uncached_usd * usd_to_cny, 4),
            "cached_equivalent_cost_cny": round(cached_usd * usd_to_cny, 4),
            "saved_cost_cny": round(saved_usd * usd_to_cny, 4),
            "cache_hit_pct": round((c_hit / p_tok) * 100.0, 1) if p_tok > 0 else 0.0,
            "avg_tools_per_req": round(t_calls / reqs, 2) if reqs > 0 else 0.0,
            "avg_completion_tokens_per_req": round(c_tok / reqs, 1) if reqs > 0 else 0.0,
            "avg_total_tokens_per_req": round(tot_tok / reqs, 1) if reqs > 0 else 0.0,
        }
    )
    return out


_LOCAL_CLIENT_QUOTA_CODES = {
    "daily_quota_exceeded",
    "total_quota_exceeded",
    "rate_limit_exceeded",
    "client_queue_full",
    "concurrency_timeout",
}


def compute_cohort_quota_metrics(
    used_requests: int,
    cohort_daily_quota: int = DEFAULT_COHORT_DAILY_QUOTA,
    cohort_reserved_quota: int = DEFAULT_COHORT_RESERVED_QUOTA,
    *,
    upstream_exhausted: bool = False,
    last_upstream_quota_error: str = "",
) -> Dict[str, Any]:
    """Compute shared Cohort daily quota percentages accounting for classmate reserve."""
    total_pool = max(10, int(cohort_daily_quota or DEFAULT_COHORT_DAILY_QUOTA))
    raw_reserved = DEFAULT_COHORT_RESERVED_QUOTA if cohort_reserved_quota is None else int(cohort_reserved_quota)
    reserved = max(0, min(total_pool - 1, raw_reserved))
    usable_budget = max(1, total_pool - reserved)
    used = max(0, int(used_requests or 0))

    usable_used_pct = round((used / usable_budget) * 100.0, 2)
    total_pool_used_pct = round((used / total_pool) * 100.0, 2)
    reserved_pool_pct = round((reserved / total_pool) * 100.0, 2)
    effective_committed_requests = used + reserved
    effective_committed_pct = round((effective_committed_requests / total_pool) * 100.0, 2)
    remaining_usable = max(0, usable_budget - used)
    dipping_into_reserve = max(0, used - usable_budget)

    if upstream_exhausted or used >= total_pool:
        status_level = "exhausted"
    elif used >= usable_budget:
        status_level = "reserve"
    elif usable_used_pct >= 80.0:
        status_level = "warn"
    else:
        status_level = "ok"

    return {
        "cohort_daily_quota": total_pool,
        "cohort_reserved_quota": reserved,
        "usable_daily_budget": usable_budget,
        "used_requests": used,
        "usable_used_pct": usable_used_pct,
        "total_pool_used_pct": total_pool_used_pct,
        "reserved_pool_pct": reserved_pool_pct,
        "effective_committed_requests": effective_committed_requests,
        "effective_committed_pct": effective_committed_pct,
        "remaining_usable_requests": remaining_usable,
        "dipping_into_reserve_requests": dipping_into_reserve,
        "reserve_consumed_requests": dipping_into_reserve,
        "upstream_quota_exhausted_today": bool(upstream_exhausted),
        "last_upstream_quota_error": last_upstream_quota_error,
        "status_level": status_level,
        "estimation_basis": {
            "metric": "discoveryengine.googleapis.com/text_answer_gen_tier_edu_regional",
            "edition": "EDU",
            "per_seat_qpd": 40,
            "estimated_seats": max(1, round(total_pool / 40)),
            "observed_max_ok_requests": 1566,
            "observed_exhaustion_requests": 1555,
        },
    }


class UsageLedger:
    """Thread-safe persistent ledger with Pacific-midnight (16:00 Shanghai) billing-day consistency."""

    def __init__(
        self,
        storage_path: Optional[Union[Path, str]] = None,
        usd_to_cny_rate: float = DEFAULT_USD_TO_CNY,
        timezone_name: str = DEFAULT_TIMEZONE,
        price_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        image_unit_usd: float = DEFAULT_IMAGE_UNIT_USD,
        video_unit_usd: float = DEFAULT_VIDEO_UNIT_USD,
        billing_timezone_name: str = DEFAULT_BILLING_TIMEZONE,
        cohort_daily_quota: int = DEFAULT_COHORT_DAILY_QUOTA,
        cohort_reserved_quota: int = DEFAULT_COHORT_RESERVED_QUOTA,
    ) -> None:
        self.storage_path: Optional[Path] = Path(storage_path) if storage_path else None
        self.usd_to_cny_rate = usd_to_cny_rate if usd_to_cny_rate > 0 else DEFAULT_USD_TO_CNY
        self.timezone_name = timezone_name or DEFAULT_TIMEZONE
        self.billing_timezone_name = billing_timezone_name or DEFAULT_BILLING_TIMEZONE
        self.cohort_daily_quota = max(10, int(cohort_daily_quota or DEFAULT_COHORT_DAILY_QUOTA))
        self.cohort_reserved_quota = max(
            0,
            min(
                self.cohort_daily_quota - 1,
                int(DEFAULT_COHORT_RESERVED_QUOTA if cohort_reserved_quota is None else cohort_reserved_quota),
            ),
        )
        self._quota_config_override: Dict[str, int] = {}
        self.price_overrides = copy.deepcopy(price_overrides) if isinstance(price_overrides, dict) else {}
        media_ov = self.price_overrides.get("_media") if isinstance(self.price_overrides.get("_media"), dict) else {}
        self.image_unit_usd = max(0.0, float(media_ov.get("image_unit_usd", image_unit_usd)))
        self.video_unit_usd = max(0.0, float(media_ov.get("video_unit_usd", video_unit_usd)))

        self._lock = threading.RLock()
        self._started_at = int(time.time())
        self._archived: Dict[str, Any] = empty_group()
        self._days: Dict[str, Dict[str, Any]] = {}
        self._session_days: Dict[str, Dict[str, Any]] = {}
        self._recent_requests: List[Dict[str, Any]] = []
        self._disk_mtime_ns: int = 0
        self._backfill_mtime_ns: int = 0
        self._load_from_disk()

    def _day_key(
        self,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        *,
        now_ts: Optional[float] = None,
    ) -> str:
        if isinstance(as_of_date, datetime):
            b_tz = resolve_timezone(self.billing_timezone_name)
            if as_of_date.tzinfo is None:
                as_of_date = as_of_date.replace(tzinfo=resolve_timezone(self.timezone_name))
            return as_of_date.astimezone(b_tz).date().isoformat()
        if isinstance(as_of_date, date):
            return as_of_date.isoformat()
        if isinstance(as_of_date, str) and as_of_date.strip():
            try:
                return date.fromisoformat(as_of_date.strip()[:10]).isoformat()
            except ValueError:
                pass
        try:
            return billing_day_key(now_ts, self.billing_timezone_name)
        except Exception:
            return datetime.now().date().isoformat()

    def update_quota_config(
        self,
        *,
        cohort_daily_quota: Optional[int] = None,
        cohort_reserved_quota: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if cohort_daily_quota is not None:
                self.cohort_daily_quota = max(10, min(500_000, int(cohort_daily_quota)))
            if cohort_reserved_quota is not None:
                self.cohort_reserved_quota = max(0, int(cohort_reserved_quota))
            self.cohort_reserved_quota = min(self.cohort_daily_quota - 1, self.cohort_reserved_quota)
            self._quota_config_override = {
                "cohort_daily_quota": self.cohort_daily_quota,
                "cohort_reserved_quota": self.cohort_reserved_quota,
            }
            self._save_to_disk_unlocked(skip_external_sync=True)
            today_key = self._day_key()
            today_reqs = int(((self._days.get(today_key) or {}).get("summary") or {}).get("requests") or 0)
            exhausted, err_msg = self._detect_upstream_quota_exhausted_unlocked(today_key)
            return compute_cohort_quota_metrics(
                today_reqs,
                self.cohort_daily_quota,
                self.cohort_reserved_quota,
                upstream_exhausted=exhausted,
                last_upstream_quota_error=err_msg,
            )

    def _detect_upstream_quota_exhausted_unlocked(self, day_key: str) -> tuple[bool, str]:
        last_ok_ts = 0
        last_quota_err_ts = 0
        last_quota_msg = ""
        for rec in self._recent_requests:
            if not isinstance(rec, dict):
                continue
            ts = int(rec.get("ts") or 0)
            rec_day = str(rec.get("day") or "")
            if not rec_day and ts > 0:
                rec_day = self._day_key(now_ts=float(ts))
            if rec_day != day_key:
                continue
            st = str(rec.get("status") or "ok")
            code = int(rec.get("status_code") or 200)
            if st == "ok" and code < 400:
                if ts >= last_ok_ts:
                    last_ok_ts = ts
                continue
            err_code = str(rec.get("error_code") or "").strip().lower()
            if err_code in _LOCAL_CLIENT_QUOTA_CODES:
                continue
            raw_msg = str(rec.get("error_message") or rec.get("reply_preview") or "").strip()
            low_msg = raw_msg.lower()
            if (
                err_code in {"quota_exceeded", "upstream_quota_exceeded", "resource_exhausted"}
                or any(
                    kw in low_msg
                    for kw in (
                        "text_answer_gen_tier",
                        "quota exceeded",
                        "quota limit",
                        "resource_exhausted",
                        "配额已用尽",
                        "额度已用完",
                    )
                )
            ):
                if ts >= last_quota_err_ts:
                    last_quota_err_ts = ts
                    last_quota_msg = raw_msg[:180]
        exhausted = bool(last_quota_err_ts > 0 and last_quota_err_ts >= last_ok_ts)
        return exhausted, (last_quota_msg if exhausted else "")

    def _backfill_sidecar_path(self) -> Optional[Path]:
        return (self.storage_path.parent / "usage_backfill.json") if self.storage_path else None

    def _merge_historical_days_unlocked(self, ext_days: Dict[str, Any]) -> bool:
        changed = False
        for dk, dv in (ext_days or {}).items():
            if not isinstance(dv, dict):
                continue
            day_str = str(dk)
            cand = sanitize_group(dv)
            reconcile_unattributed_host_tenant(cand)
            cand_reqs = int((cand.get("summary") or {}).get("requests") or 0)
            if cand_reqs <= 0:
                continue
            cur = self._days.get(day_str)
            cur_reqs = int(((cur or {}).get("summary") or {}).get("requests") or 0) if cur else 0
            if cur is None or cand_reqs > cur_reqs:
                self._days[day_str] = cand
                changed = True
            elif cur is not None and reconcile_unattributed_host_tenant(cur):
                changed = True
        return changed

    def _sync_external_sources_unlocked(self) -> bool:
        changed = False
        if self.storage_path is not None and self.storage_path.is_file():
            try:
                st_ns = self.storage_path.stat().st_mtime_ns
                if self._disk_mtime_ns and st_ns > self._disk_mtime_ns:
                    raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and isinstance(raw.get("days"), dict):
                        if self._merge_historical_days_unlocked(raw["days"]):
                            changed = True
                    self._disk_mtime_ns = st_ns
            except Exception as exc:
                logger.debug("同步外部账本变更失败: %s", exc)
        sidecar = self._backfill_sidecar_path()
        if sidecar is not None and sidecar.is_file():
            try:
                bf_ns = sidecar.stat().st_mtime_ns
                if bf_ns != self._backfill_mtime_ns:
                    bf_raw = json.loads(sidecar.read_text(encoding="utf-8"))
                    bf_days = bf_raw.get("days") if isinstance(bf_raw, dict) else None
                    if isinstance(bf_days, dict) and self._merge_historical_days_unlocked(bf_days):
                        changed = True
                    self._backfill_mtime_ns = bf_ns
            except Exception as exc:
                logger.debug("读取历史回测账本 %s 失败: %s", sidecar, exc)
        for dg in self._days.values():
            if reconcile_unattributed_host_tenant(dg):
                changed = True
        return changed

    def _load_from_disk(self) -> None:
        dirty = False
        if self.storage_path is not None and self.storage_path.is_file():
            try:
                st_ns = self.storage_path.stat().st_mtime_ns
                raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw_ver = int(raw.get("version") or 1)
                    q_cfg = raw.get("quota_config")
                    if isinstance(q_cfg, dict):
                        try:
                            if q_cfg.get("cohort_daily_quota") is not None:
                                self.cohort_daily_quota = max(10, min(500_000, int(q_cfg["cohort_daily_quota"])))
                            if q_cfg.get("cohort_reserved_quota") is not None:
                                self.cohort_reserved_quota = max(
                                    0,
                                    min(self.cohort_daily_quota - 1, int(q_cfg["cohort_reserved_quota"])),
                                )
                            self._quota_config_override = {
                                "cohort_daily_quota": self.cohort_daily_quota,
                                "cohort_reserved_quota": self.cohort_reserved_quota,
                            }
                        except (TypeError, ValueError):
                            pass
                    self._archived = sanitize_group(raw.get("archived"))
                    reconcile_unattributed_host_tenant(self._archived)
                    if isinstance(raw.get("days"), dict):
                        for dk, dv in raw["days"].items():
                            if isinstance(dv, dict):
                                grp = sanitize_group(dv)
                                if reconcile_unattributed_host_tenant(grp):
                                    dirty = True
                                self._days[str(dk)] = grp
                    if isinstance(raw.get("recent_requests"), list):
                        loaded_recents: List[Dict[str, Any]] = []
                        for x in raw["recent_requests"][-MAX_RECENT_REQUESTS:]:
                            if not isinstance(x, dict):
                                continue
                            item = dict(x)
                            if raw_ver < LEDGER_SCHEMA_VERSION and isinstance(item.get("ts"), (int, float)) and item["ts"] > 0:
                                new_dk = self._day_key(now_ts=float(item["ts"]))
                                if item.get("day") != new_dk:
                                    item["day"] = new_dk
                                    dirty = True
                            loaded_recents.append(item)
                        self._recent_requests = loaded_recents
                self._disk_mtime_ns = st_ns
            except Exception as exc:
                logger.warning("读取本地账本 %s 失败（将重新初始化）: %s", self.storage_path, exc)
        if self._sync_external_sources_unlocked():
            dirty = True
        if dirty and self.storage_path is not None and self.storage_path.is_file():
            self._save_to_disk_unlocked(skip_external_sync=True)

    def _save_to_disk_unlocked(self, skip_external_sync: bool = False) -> None:
        if self.storage_path is None:
            return
        try:
            if not skip_external_sync:
                self._sync_external_sources_unlocked()
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._days) > MAX_STORED_DAYS:
                sorted_keys = sorted(self._days.keys())
                to_archive = sorted_keys[:-MAX_STORED_DAYS]
                for old_k in to_archive:
                    old_group = self._days.pop(old_k, None)
                    if old_group:
                        merge_group_into(self._archived, old_group)
            payload: Dict[str, Any] = {
                "version": LEDGER_SCHEMA_VERSION,
                "billing_timezone": self.billing_timezone_name,
                "updated_at": int(time.time()),
                "archived": self._archived,
                "days": self._days,
                "recent_requests": self._recent_requests[-MAX_RECENT_REQUESTS:],
            }
            if self._quota_config_override:
                payload["quota_config"] = dict(self._quota_config_override)
            atomic_write_json(self.storage_path, payload, mode=0o600, dir_mode=0o755)
            if self.storage_path.is_file():
                self._disk_mtime_ns = self.storage_path.stat().st_mtime_ns
        except Exception as exc:
            logger.warning("写入本地账本 %s 失败: %s", self.storage_path, exc)

    def record_request(
        self,
        requested_model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        browser_active_model: Optional[str] = None,
        gcloud_fallback_model: Optional[str] = None,
        task_type: str = "chat",
        strategy: str = "smart_single_turn",
        cached_tokens: int = 0,
        delta_prompt_tokens: Optional[int] = None,
        images_generated: int = 0,
        videos_generated: int = 0,
        audio_requests: int = 0,
        slot_key: str = "",
        tab_id: Optional[int] = None,
        context_tokens: Optional[int] = None,
        context_limit: Optional[int] = None,
        duration_ms: int = 0,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        tenant_id: str = "host",
        tenant_name: str = "本机直连",
        request_id: str = "",
        session_id: str = "",
        session_title: str = "",
        prompt_preview: str = "",
        reply_preview: str = "",
        client_ip: str = "",
        tool_calls_count: int = 0,
    ) -> Dict[str, Any]:
        """Record one completed task into daily, session, model, and task ledgers."""
        p_tokens = max(0, int(prompt_tokens or 0))
        c_tokens = max(0, int(completion_tokens or 0))
        norm_task = task_type if task_type in TASK_LABELS else "chat"
        img_cnt = max(0, int(images_generated or 0))
        vid_cnt = max(0, int(videos_generated or 0))
        aud_cnt = max(0, int(audio_requests or (1 if norm_task == "audio" else 0)))
        cmp_cnt = 1 if norm_task == "compaction" else 0

        if strategy == "smart_incremental_delta" and cached_tokens > 0:
            eff_cached = min(p_tokens, max(0, int(cached_tokens)))
            if delta_prompt_tokens is not None:
                eff_delta = max(0, int(delta_prompt_tokens))
                if eff_cached + eff_delta != p_tokens:
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
            overrides=self.price_overrides,
        )

        media_usd = (img_cnt * self.image_unit_usd) + (vid_cnt * self.video_unit_usd)
        uncached_usd = (
            (p_tokens / 1_000_000.0) * rate.input_per_1m
            + (c_tokens / 1_000_000.0) * rate.output_per_1m
            + media_usd
        )
        cached_usd = (
            (eff_cached / 1_000_000.0) * rate.cached_input_per_1m
            + (eff_delta / 1_000_000.0) * rate.input_per_1m
            + (c_tokens / 1_000_000.0) * rate.output_per_1m
            + media_usd
        )
        saved_usd = max(0.0, uncached_usd - cached_usd)

        ctx_limit = int(context_limit or rate.context_window or DEFAULT_CONTEXT_WINDOW)
        ctx_tokens = int(context_tokens if context_tokens is not None else (p_tokens + c_tokens))
        util_pct = round(min(100.0, (ctx_tokens / max(ctx_limit, 1)) * 100.0), 2)
        now_ts = int(time.time())
        day_key = self._day_key(as_of_date, now_ts=float(now_ts))
        eff_tid = (tenant_id or "host").strip() or "host"
        eff_tname = (tenant_name or "本机直连").strip() or "本机直连"

        eff_tool_calls = max(0, int(tool_calls_count or 0))
        entry_delta: Dict[str, Any] = {
            "requests": 1,
            "tool_calls": eff_tool_calls,
            "prompt_tokens": p_tokens,
            "cached_tokens": eff_cached,
            "delta_prompt_tokens": eff_delta,
            "completion_tokens": c_tokens,
            "total_tokens": p_tokens + c_tokens,
            "images_generated": img_cnt,
            "videos_generated": vid_cnt,
            "audio_requests": aud_cnt,
            "compactions": cmp_cnt,
            "equivalent_cost_usd": round(uncached_usd, 8),
            "cached_equivalent_cost_usd": round(cached_usd, 8),
        }
        single_tenant_group = {
            "tenant_name": eff_tname,
            "errors": 0,
            "summary": dict(entry_delta),
            "by_model": {canonical_model: dict(entry_delta)},
            "by_task": {norm_task: dict(entry_delta)},
        }
        single_group = {
            "summary": entry_delta,
            "by_model": {canonical_model: dict(entry_delta)},
            "by_task": {norm_task: dict(entry_delta)},
            "by_tenant": {eff_tid: single_tenant_group},
        }

        record = {
            "request_id": (request_id or "").strip(),
            "ts": now_ts,
            "day": day_key,
            "status": "ok",
            "status_code": 200,
            "slot_key": slot_key,
            "tab_id": tab_id,
            "tenant_id": eff_tid,
            "tenant_name": eff_tname,
            "client_ip": (client_ip or "").strip(),
            "session_id": (session_id or "").strip()[:100],
            "session_title": (session_title or "").strip()[:120],
            "prompt_preview": (prompt_preview or "").strip()[:160],
            "reply_preview": (reply_preview or "").strip()[:180],
            "tool_calls_count": max(0, int(tool_calls_count or 0)),
            "task_type": norm_task,
            "task_label": TASK_LABELS.get(norm_task, norm_task),
            "requested_model": requested_model,
            "model": canonical_model,
            "display_name": rate.display_name,
            "strategy": strategy,
            "prompt_tokens": p_tokens,
            "cached_tokens": eff_cached,
            "delta_prompt_tokens": eff_delta,
            "completion_tokens": c_tokens,
            "total_tokens": p_tokens + c_tokens,
            "images_generated": img_cnt,
            "videos_generated": vid_cnt,
            "audio_requests": aud_cnt,
            "compactions": cmp_cnt,
            "context_tokens": ctx_tokens,
            "context_limit": ctx_limit,
            "context_utilization_pct": util_pct,
            "equivalent_cost_usd": round(uncached_usd, 6),
            "cached_equivalent_cost_usd": round(cached_usd, 6),
            "saved_cost_usd": round(saved_usd, 6),
            "equivalent_cost_cny": round(uncached_usd * self.usd_to_cny_rate, 4),
            "cached_equivalent_cost_cny": round(cached_usd * self.usd_to_cny_rate, 4),
            "saved_cost_cny": round(saved_usd * self.usd_to_cny_rate, 4),
            "input_rate_per_1m": rate.input_per_1m,
            "cached_input_rate_per_1m": rate.cached_input_per_1m,
            "output_rate_per_1m": rate.output_per_1m,
            "duration_ms": max(0, int(duration_ms or 0)),
        }

        with self._lock:
            day_group = self._days.setdefault(day_key, empty_group())
            merge_group_into(day_group, single_group)

            sess_day_group = self._session_days.setdefault(day_key, empty_group())
            merge_group_into(sess_day_group, single_group)

            self._recent_requests.append(record)
            if len(self._recent_requests) > MAX_RECENT_REQUESTS:
                self._recent_requests = self._recent_requests[-MAX_RECENT_REQUESTS:]
            self._save_to_disk_unlocked()

        return record

    def _enrich_group(self, group: Dict[str, Any]) -> Dict[str, Any]:
        by_model_out: Dict[str, Dict[str, Any]] = {}
        sorted_models = sorted(
            (group.get("by_model") or {}).items(),
            key=lambda kv: (
                float(kv[1].get("cached_equivalent_cost_usd") or 0.0),
                float(kv[1].get("equivalent_cost_usd") or 0.0),
                int(kv[1].get("total_tokens") or 0),
            ),
            reverse=True,
        )
        for m_key, m_bucket in sorted_models:
            m_rate = get_model_pricing(m_key, prompt_tokens=0, overrides=self.price_overrides)
            enriched = enrich_bucket(m_bucket, self.usd_to_cny_rate)
            enriched["display_name"] = m_rate.display_name
            enriched["input_rate_per_1m"] = m_rate.input_per_1m
            enriched["cached_input_rate_per_1m"] = m_rate.cached_input_per_1m
            enriched["output_rate_per_1m"] = m_rate.output_per_1m
            enriched["rate_note"] = m_rate.rate_note
            by_model_out[m_key] = enriched

        by_task_out: Dict[str, Dict[str, Any]] = {}
        raw_tasks = group.get("by_task") or {}
        for t_key in TASK_LABELS:
            if t_key in raw_tasks:
                enriched_t = enrich_bucket(raw_tasks[t_key], self.usd_to_cny_rate)
                enriched_t["label"] = TASK_LABELS[t_key]
                by_task_out[t_key] = enriched_t
        for t_key, t_bucket in raw_tasks.items():
            if t_key not in by_task_out:
                enriched_t = enrich_bucket(t_bucket, self.usd_to_cny_rate)
                enriched_t["label"] = TASK_LABELS.get(t_key, t_key)
                by_task_out[t_key] = enriched_t

        by_tenant_out: Dict[str, Dict[str, Any]] = {}
        sorted_tenants = sorted(
            (group.get("by_tenant") or {}).items(),
            key=lambda kv: (
                float((kv[1].get("summary") or {}).get("cached_equivalent_cost_usd") or 0.0),
                int((kv[1].get("summary") or {}).get("requests") or 0),
                int(kv[1].get("errors") or 0),
            ),
            reverse=True,
        )
        for tid, t_raw in sorted_tenants:
            tm_out: Dict[str, Dict[str, Any]] = {}
            for m_key, m_bucket in (t_raw.get("by_model") or {}).items():
                m_rate = get_model_pricing(m_key, prompt_tokens=0, overrides=self.price_overrides)
                en_m = enrich_bucket(m_bucket, self.usd_to_cny_rate)
                en_m["display_name"] = m_rate.display_name
                en_m["input_rate_per_1m"] = m_rate.input_per_1m
                en_m["cached_input_rate_per_1m"] = m_rate.cached_input_per_1m
                en_m["output_rate_per_1m"] = m_rate.output_per_1m
                en_m["rate_note"] = m_rate.rate_note
                tm_out[m_key] = en_m
            tt_out: Dict[str, Dict[str, Any]] = {}
            for t_key, t_bucket in (t_raw.get("by_task") or {}).items():
                en_t = enrich_bucket(t_bucket, self.usd_to_cny_rate)
                en_t["label"] = TASK_LABELS.get(t_key, t_key)
                tt_out[t_key] = en_t
            t_sum = enrich_bucket(t_raw.get("summary") or empty_bucket(), self.usd_to_cny_rate)
            t_sum["errors"] = int(t_raw.get("errors") or 0)
            by_tenant_out[tid] = {
                "tenant_id": tid,
                "tenant_name": str(t_raw.get("tenant_name") or ("本机直连" if tid == "host" else tid)),
                "errors": int(t_raw.get("errors") or 0),
                "summary": t_sum,
                "by_model": tm_out,
                "by_task": tt_out,
            }

        return {
            "summary": enrich_bucket(group.get("summary") or empty_bucket(), self.usd_to_cny_rate),
            "by_model": by_model_out,
            "by_task": by_task_out,
            "by_tenant": by_tenant_out,
        }

    def record_failed_request(
        self,
        *,
        requested_model: str = "",
        task_type: str = "chat",
        tenant_id: str = "host",
        tenant_name: str = "本机直连",
        client_ip: str = "",
        session_id: str = "",
        prompt_preview: str = "",
        status: str = "error",
        status_code: int = 500,
        error_code: str = "",
        error_message: str = "",
        slot_key: str = "",
        tab_id: Optional[int] = None,
        duration_ms: int = 0,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        request_id: str = "",
    ) -> Dict[str, Any]:
        """Record a blocked, rate-limited, or failed request into the task audit log without billing tokens."""
        norm_task = task_type if task_type in TASK_LABELS else "chat"
        now_ts = int(time.time())
        day_key = self._day_key(as_of_date, now_ts=float(now_ts))
        eff_tid = (tenant_id or "host").strip() or "host"
        eff_tname = (tenant_name or "本机直连").strip() or "本机直连"
        canonical_model = resolve_canonical_billing_model(requested_model=requested_model)
        rate = get_model_pricing(canonical_model=canonical_model, prompt_tokens=0, overrides=self.price_overrides)
        record = {
            "request_id": (request_id or "").strip(),
            "ts": now_ts,
            "day": day_key,
            "status": status or "error",
            "status_code": int(status_code or 500),
            "error_code": (error_code or "").strip(),
            "error_message": (error_message or "").strip()[:220],
            "slot_key": slot_key,
            "tab_id": tab_id,
            "tenant_id": eff_tid,
            "tenant_name": eff_tname,
            "client_ip": (client_ip or "").strip(),
            "session_id": (session_id or "").strip()[:100],
            "session_title": "",
            "prompt_preview": (prompt_preview or "").strip()[:160],
            "reply_preview": (error_message or "").strip()[:180],
            "tool_calls_count": 0,
            "task_type": norm_task,
            "task_label": TASK_LABELS.get(norm_task, norm_task),
            "requested_model": requested_model,
            "model": canonical_model,
            "display_name": rate.display_name,
            "strategy": "rejected" if int(status_code or 500) in {401, 403, 429} else "failed",
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "delta_prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "images_generated": 0,
            "videos_generated": 0,
            "audio_requests": 0,
            "compactions": 0,
            "context_tokens": 0,
            "context_limit": rate.context_window,
            "context_utilization_pct": 0.0,
            "equivalent_cost_usd": 0.0,
            "cached_equivalent_cost_usd": 0.0,
            "saved_cost_usd": 0.0,
            "equivalent_cost_cny": 0.0,
            "cached_equivalent_cost_cny": 0.0,
            "saved_cost_cny": 0.0,
            "duration_ms": max(0, int(duration_ms or 0)),
        }
        with self._lock:
            day_group = self._days.setdefault(day_key, empty_group())
            t_dst = day_group.setdefault("by_tenant", {}).setdefault(eff_tid, empty_tenant_group(eff_tname))
            t_dst["tenant_name"] = eff_tname
            t_dst["errors"] = int(t_dst.get("errors") or 0) + 1
            sess_day_group = self._session_days.setdefault(day_key, empty_group())
            st_dst = sess_day_group.setdefault("by_tenant", {}).setdefault(eff_tid, empty_tenant_group(eff_tname))
            st_dst["tenant_name"] = eff_tname
            st_dst["errors"] = int(st_dst.get("errors") or 0) + 1
            self._recent_requests.append(record)
            if len(self._recent_requests) > MAX_RECENT_REQUESTS:
                self._recent_requests = self._recent_requests[-MAX_RECENT_REQUESTS:]
            self._save_to_disk_unlocked()
        return record

    def snapshot(
        self,
        active_model_hint: Optional[str] = None,
        default_model: Optional[str] = None,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        include_catalog: bool = False,
    ) -> Dict[str, Any]:
        """Build a consistent snapshot across all time windows, models, tasks, and daily series."""
        now_ts = time.time()
        day_key = self._day_key(as_of_date, now_ts=now_ts)
        try:
            anchor_date = date.fromisoformat(day_key)
        except ValueError:
            anchor_date = datetime.now().date()
        yesterday_key = (anchor_date - timedelta(days=1)).isoformat()
        start_7d_key = (anchor_date - timedelta(days=6)).isoformat()
        start_30d_key = (anchor_date - timedelta(days=29)).isoformat()

        billing_info = billing_window_info(
            now_ts=now_ts,
            billing_tz_name=self.billing_timezone_name,
            local_tz_name=self.timezone_name,
        )
        if as_of_date is not None:
            billing_info["billing_day"] = day_key
            billing_info["yesterday_billing_day"] = yesterday_key

        canonical_active = resolve_canonical_billing_model(
            default_model if not active_model_hint else None,
            active_model_hint,
        )
        active_rate = get_model_pricing(
            canonical_active, prompt_tokens=0, overrides=self.price_overrides
        )

        with self._lock:
            self._sync_external_sources_unlocked()
            total_group = empty_group()
            merge_group_into(total_group, self._archived)
            for dg in self._days.values():
                merge_group_into(total_group, dg)

            today_group = self._days.get(day_key) or empty_group()
            yesterday_group = self._days.get(yesterday_key) or empty_group()

            last_7d_group = empty_group()
            last_30d_group = empty_group()
            for dk, dg in self._days.items():
                if start_7d_key <= dk <= day_key:
                    merge_group_into(last_7d_group, dg)
                if start_30d_key <= dk <= day_key:
                    merge_group_into(last_30d_group, dg)

            session_group = empty_group()
            for sg in self._session_days.values():
                merge_group_into(session_group, sg)

            all_enriched = self._enrich_group(total_group)
            today_enriched = self._enrich_group(today_group)
            yesterday_enriched = self._enrich_group(yesterday_group)
            last_7d_enriched = self._enrich_group(last_7d_group)
            last_30d_enriched = self._enrich_group(last_30d_group)
            session_enriched = self._enrich_group(session_group)

            exhausted_today, exhausted_msg = self._detect_upstream_quota_exhausted_unlocked(day_key)
            cohort_quota = compute_cohort_quota_metrics(
                int(today_enriched["summary"].get("requests") or 0),
                self.cohort_daily_quota,
                self.cohort_reserved_quota,
                upstream_exhausted=exhausted_today,
                last_upstream_quota_error=exhausted_msg,
            )
            today_enriched["summary"]["usable_quota_pct"] = cohort_quota["usable_used_pct"]
            today_enriched["summary"]["total_pool_pct"] = cohort_quota["total_pool_used_pct"]
            yest_q = compute_cohort_quota_metrics(
                int(yesterday_enriched["summary"].get("requests") or 0),
                self.cohort_daily_quota,
                self.cohort_reserved_quota,
            )
            yesterday_enriched["summary"]["usable_quota_pct"] = yest_q["usable_used_pct"]
            yesterday_enriched["summary"]["total_pool_pct"] = yest_q["total_pool_used_pct"]

            by_day_recent: Dict[str, Dict[str, Any]] = {}
            daily_details: Dict[str, Dict[str, Any]] = {}
            sorted_days_desc = sorted(self._days.keys(), reverse=True)
            for dk in sorted_days_desc[:14]:
                eb = enrich_bucket(self._days[dk]["summary"], self.usd_to_cny_rate)
                dq = compute_cohort_quota_metrics(
                    int(eb.get("requests") or 0),
                    self.cohort_daily_quota,
                    self.cohort_reserved_quota,
                )
                eb["usable_quota_pct"] = dq["usable_used_pct"]
                eb["total_pool_pct"] = dq["total_pool_used_pct"]
                by_day_recent[dk] = eb
            for dk in sorted_days_desc:
                eg = self._enrich_group(self._days[dk])
                dq = compute_cohort_quota_metrics(
                    int(eg["summary"].get("requests") or 0),
                    self.cohort_daily_quota,
                    self.cohort_reserved_quota,
                    upstream_exhausted=(exhausted_today if dk == day_key else False),
                )
                eg["summary"]["usable_quota_pct"] = dq["usable_used_pct"]
                eg["summary"]["total_pool_pct"] = dq["total_pool_used_pct"]
                eg["quota"] = dq
                daily_details[dk] = eg

            result: Dict[str, Any] = {
                "token_count_source": "local_estimate_not_google_billing",
                "today_date": day_key,
                "yesterday_date": yesterday_key,
                "timezone": self.timezone_name,
                "billing_timezone": self.billing_timezone_name,
                "billing_cycle": billing_info,
                "cohort_quota": cohort_quota,
                "started_at": self._started_at,
                "usd_to_cny_rate": self.usd_to_cny_rate,
                "media_unit_rates_usd": {
                    "image": self.image_unit_usd,
                    "video": self.video_unit_usd,
                },
                "active_pricing": active_rate.to_dict(),
                "all_time": all_enriched["summary"],
                "today": today_enriched["summary"],
                "yesterday": yesterday_enriched["summary"],
                "last_7d": last_7d_enriched["summary"],
                "last_30d": last_30d_enriched["summary"],
                "current_session": session_enriched["summary"],
                "by_model": all_enriched["by_model"],
                "by_task": all_enriched["by_task"],
                "by_tenant": all_enriched["by_tenant"],
                "periods": {
                    "all": all_enriched,
                    "today": today_enriched,
                    "yesterday": yesterday_enriched,
                    "7d": last_7d_enriched,
                    "30d": last_30d_enriched,
                    "session": session_enriched,
                },
                "daily": daily_details,
                "recent_days": by_day_recent,
                "recent_requests": list(reversed(self._recent_requests[-MAX_RECENT_REQUESTS:])),
            }
            if include_catalog:
                result["pricing_catalog"] = list_pricing_catalog(overrides=self.price_overrides)
            return result


def backfill_pre_update_usage(
    ledger_path: Union[Path, str],
    legacy_stats_path: Optional[Union[Path, str]] = None,
    copilot_dir: Optional[Union[Path, str]] = None,
    cutoff_day: str = "2026-11-17",
    bridge_provider_ids: Sequence[str] = ("3126a75e-77bb-4baa-b663-6abcc2cdb5a7",),
    excluded_provider_ids: Sequence[str] = (
        "695be4fc-bb13-4327-8c59-6bd91242b42d",
        "9877533f-9a89-4594-b562-9f70eea0ee41",
    ),
    price_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Backtest and persist lost pre-major-update usage from legacy stats & local BYOK history."""
    import sqlite3

    l_path = Path(ledger_path)
    root_dir = l_path.resolve().parent.parent if l_path.parent.name == ".cache" else l_path.resolve().parent
    us_path = Path(legacy_stats_path) if legacy_stats_path else (root_dir / "usage_stats.json")
    cp_dir = Path(copilot_dir) if copilot_dir else (Path.home() / ".copilot")
    tz_obj = resolve_timezone(DEFAULT_BILLING_TIMEZONE)

    reconstructed_days: Dict[str, Dict[str, Any]] = {}

    # 1. Reconstruct turn-by-turn BYOK usage from local Copilot session store (< cutoff_day)
    ss_db = cp_dir / "session-store.db"
    data_db = cp_dir / "data.db"
    sess_pid: Dict[str, Optional[str]] = {}
    if data_db.is_file():
        try:
            con_d = sqlite3.connect(f"file:{data_db}?mode=ro", uri=True)
            for sid, pid in con_d.execute("SELECT id, provider_id FROM sessions").fetchall():
                sess_pid[str(sid)] = str(pid) if pid else None
            con_d.close()
        except Exception as exc:
            logger.warning("读取 Copilot data.db 失败: %s", exc)

    if ss_db.is_file():
        try:
            con_s = sqlite3.connect(f"file:{ss_db}?mode=ro", uri=True)
            rows = con_s.execute(
                """
                SELECT session_id, created_at, model, api_endpoint, input_tokens, output_tokens
                FROM assistant_usage_events
                WHERE token_details_json IS NULL
                  AND (total_nano_aiu IS NULL OR total_nano_aiu = 0)
                  AND lower(COALESCE(model, '')) LIKE '%gemini%'
                ORDER BY created_at, id
                """
            ).fetchall()
            con_s.close()
            excluded_set = set(excluded_provider_ids)
            bridge_set = set(bridge_provider_ids)
            sess_state: Dict[str, Dict[str, Any]] = {}
            for sid_raw, ca_raw, m_raw, ep_raw, inp_raw, out_raw in rows:
                sid = str(sid_raw or "")
                pid = sess_pid.get(sid)
                m_str = str(m_raw or "")
                if pid in excluded_set:
                    continue
                if bridge_set and pid is not None and pid not in bridge_set and not m_str.lower().startswith("gemini-enterprise"):
                    continue
                s_ca = str(ca_raw or "").strip()
                if not s_ca:
                    continue
                dt = datetime.fromisoformat((s_ca.replace("Z", "+00:00") if "T" in s_ca else s_ca + "+00:00")).astimezone(tz_obj)
                dk = dt.strftime("%Y-%m-%d")
                if dk >= cutoff_day:
                    continue
                ts = dt.timestamp()
                p_tok = max(0, int(inp_raw or 0))
                c_tok = max(0, int(out_raw or 0))
                canon_m = resolve_canonical_billing_model(m_str)
                task_k = "responses" if str(ep_raw or "").strip() == "/responses" else "chat"
                prev = sess_state.get(sid)
                if (
                    prev
                    and prev["model"] == canon_m
                    and p_tok >= prev["p"] * 0.85
                    and prev["ctx"] < DEFAULT_CONTEXT_WINDOW
                    and (ts - prev["ts"]) <= 1800
                ):
                    eff_cached = min(p_tok, prev["ctx"])
                    eff_delta = max(0, p_tok - eff_cached)
                else:
                    eff_cached = 0
                    eff_delta = p_tok
                sess_state[sid] = {"p": p_tok, "ctx": p_tok + c_tok, "ts": ts, "model": canon_m}
                rate = get_model_pricing(canon_m, prompt_tokens=p_tok, overrides=price_overrides)
                uncached_usd = (p_tok / 1_000_000.0) * rate.input_per_1m + (c_tok / 1_000_000.0) * rate.output_per_1m
                cached_usd = (
                    (eff_cached / 1_000_000.0) * rate.cached_input_per_1m
                    + (eff_delta / 1_000_000.0) * rate.input_per_1m
                    + (c_tok / 1_000_000.0) * rate.output_per_1m
                )
                delta_b = {
                    "requests": 1,
                    "prompt_tokens": p_tok,
                    "cached_tokens": eff_cached,
                    "delta_prompt_tokens": eff_delta,
                    "completion_tokens": c_tok,
                    "total_tokens": p_tok + c_tok,
                    "images_generated": 0,
                    "videos_generated": 0,
                    "audio_requests": 0,
                    "compactions": 0,
                    "equivalent_cost_usd": round(uncached_usd, 8),
                    "cached_equivalent_cost_usd": round(cached_usd, 8),
                }
                d_grp = reconstructed_days.setdefault(dk, empty_group())
                merge_group_into(
                    d_grp,
                    {
                        "summary": delta_b,
                        "by_model": {canon_m: dict(delta_b)},
                        "by_task": {task_k: dict(delta_b)},
                        "by_tenant": {
                            "host": {
                                "tenant_name": "本机直连",
                                "errors": 0,
                                "summary": dict(delta_b),
                                "by_model": {canon_m: dict(delta_b)},
                                "by_task": {task_k: dict(delta_b)},
                            }
                        },
                    },
                )
        except Exception as exc:
            logger.warning("回测 Copilot 会话历史失败: %s", exc)

    # 2. Overlay exact server-measured day buckets from legacy usage_stats.json where complete
    if us_path.is_file():
        try:
            us_raw = json.loads(us_path.read_text(encoding="utf-8"))
            us_by_day = (us_raw.get("by_day") or {}) if isinstance(us_raw, dict) else {}
            us_by_model = (us_raw.get("by_model") or {}) if isinstance(us_raw, dict) else {}
            for dk, us_b_raw in us_by_day.items():
                if str(dk) >= cutoff_day or not isinstance(us_b_raw, dict):
                    continue
                us_b = sanitize_bucket(us_b_raw)
                us_reqs = int(us_b.get("requests") or 0)
                if us_reqs <= 0:
                    continue
                sim_grp = reconstructed_days.get(str(dk))
                sim_reqs = int(((sim_grp or {}).get("summary") or {}).get("requests") or 0)
                if sim_grp is None or us_reqs >= sim_reqs:
                    # Preserve exact legacy day totals while keeping model/task split proportions
                    new_grp = empty_group()
                    new_grp["summary"] = dict(us_b)
                    if sim_grp and sim_reqs > 0:
                        # Preserve task split (e.g. /responses vs /chat/completions) scaled to exact us_b
                        t_items = list((sim_grp.get("by_task") or {}).items())
                        if len(t_items) == 1:
                            new_grp["by_task"][t_items[0][0]] = dict(us_b)
                        else:
                            acc_t = empty_bucket()
                            for idx, (tk, tb) in enumerate(t_items):
                                if idx == len(t_items) - 1:
                                    rem_tb = _subtract_bucket(us_b, acc_t)
                                    new_grp["by_task"][tk] = rem_tb
                                else:
                                    ratio = int(tb.get("requests") or 0) / max(1, sim_reqs)
                                    part = empty_bucket()
                                    part["requests"] = int(tb.get("requests") or 0)
                                    for k in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                                        part[k] = int(round(us_b[k] * ratio))
                                    part["delta_prompt_tokens"] = max(0, part["prompt_tokens"] - part["cached_tokens"])
                                    part["total_tokens"] = part["prompt_tokens"] + part["completion_tokens"]
                                    part["equivalent_cost_usd"] = round(us_b["equivalent_cost_usd"] * ratio, 8)
                                    part["cached_equivalent_cost_usd"] = round(us_b["cached_equivalent_cost_usd"] * ratio, 8)
                                    new_grp["by_task"][tk] = part
                                    merge_bucket_into(acc_t, part)
                    else:
                        new_grp["by_task"]["chat"] = dict(us_b)

                    # Check if legacy flash/argon single probe belongs to this day
                    flash_b = sanitize_bucket(us_by_model.get("gemini-3.8-flash")) if "gemini-3.8-flash" in us_by_model else None
                    if str(dk) == "2026-11-16" and flash_b and int(flash_b.get("requests") or 0) == 1 and us_reqs > int(flash_b["requests"]):
                        new_grp["by_model"]["gemini-3.8-flash"] = dict(flash_b)
                        new_grp["by_model"]["gemini-4-carbon"] = _subtract_bucket(us_b, flash_b)
                    else:
                        new_grp["by_model"]["gemini-4-carbon"] = dict(us_b)
                    reconcile_unattributed_host_tenant(new_grp)
                    reconstructed_days[str(dk)] = new_grp
        except Exception as exc:
            logger.warning("合并旧版 usage_stats.json 失败: %s", exc)

    sidecar_path = l_path.parent / "usage_backfill.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_payload = {
        "version": LEDGER_SCHEMA_VERSION,
        "billing_timezone": DEFAULT_BILLING_TIMEZONE,
        "generated_at": int(time.time()),
        "cutoff_day": cutoff_day,
        "days": {k: reconstructed_days[k] for k in sorted(reconstructed_days.keys())},
    }
    atomic_write_json(sidecar_path, sidecar_payload, mode=0o600, dir_mode=0o755)

    ledger = UsageLedger(storage_path=l_path, price_overrides=price_overrides)
    with ledger._lock:
        ledger._merge_historical_days_unlocked(reconstructed_days)
        ledger._save_to_disk_unlocked(skip_external_sync=True)
    return {
        "backfilled_days": sorted(reconstructed_days.keys()),
        "sidecar_path": str(sidecar_path),
        "ledger_path": str(l_path),
        "snapshot_all_time": ledger.snapshot()["all_time"],
    }


def migrate_ledger_to_billing_days(
    ledger_path: Union[Path, str],
    copilot_dir: Optional[Union[Path, str]] = None,
    cutoff_day: str = "2026-11-17",
    bridge_provider_ids: Sequence[str] = ("3126a75e-77bb-4baa-b663-6abcc2cdb5a7",),
    excluded_provider_ids: Sequence[str] = (
        "695be4fc-bb13-4327-8c59-6bd91242b42d",
        "9877533f-9a89-4594-b562-9f70eea0ee41",
    ),
    price_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Re-bucket historical local usage into 00:00 America/Los_Angeles (16:00 Shanghai) billing days."""
    import sqlite3

    l_path = Path(ledger_path)
    cp_dir = Path(copilot_dir) if copilot_dir else (Path.home() / ".copilot")
    b_tz = resolve_timezone(DEFAULT_BILLING_TIMEZONE)
    img_usd = DEFAULT_IMAGE_UNIT_USD
    vid_usd = DEFAULT_VIDEO_UNIT_USD

    old_raw: Dict[str, Any] = {}
    if l_path.is_file():
        try:
            old_raw = json.loads(l_path.read_text(encoding="utf-8"))
        except Exception:
            old_raw = {}

    old_days = (old_raw.get("days") or {}) if isinstance(old_raw, dict) else {}
    old_recents = (old_raw.get("recent_requests") or []) if isinstance(old_raw, dict) else []
    quota_cfg = old_raw.get("quota_config") if isinstance(old_raw, dict) else None

    # Reconstruct timestamp-accurate billing days from Copilot session-store.db
    rebuilt_days: Dict[str, Dict[str, Any]] = {}
    copilot_shanghai_days: Dict[str, Dict[str, Any]] = {}
    sh_tz = resolve_timezone(DEFAULT_TIMEZONE)
    ss_db = cp_dir / "session-store.db"
    data_db = cp_dir / "data.db"
    sess_pid: Dict[str, Optional[str]] = {}
    if data_db.is_file():
        try:
            con_d = sqlite3.connect(f"file:{data_db}?mode=ro", uri=True)
            for sid, pid in con_d.execute("SELECT id, provider_id FROM sessions").fetchall():
                sess_pid[str(sid)] = str(pid) if pid else None
            con_d.close()
        except Exception as exc:
            logger.warning("读取 Copilot data.db 失败: %s", exc)

    max_copilot_ts_by_day: Dict[str, float] = {}
    if ss_db.is_file():
        con_s = sqlite3.connect(f"file:{ss_db}?mode=ro", uri=True)
        rows = con_s.execute(
            """
            SELECT session_id, created_at, model, api_endpoint, input_tokens, output_tokens
            FROM assistant_usage_events
            WHERE token_details_json IS NULL
              AND (total_nano_aiu IS NULL OR total_nano_aiu = 0)
              AND lower(COALESCE(model, '')) LIKE '%gemini%'
            ORDER BY created_at, id
            """
        ).fetchall()
        con_s.close()
        excluded_set = set(excluded_provider_ids)
        bridge_set = set(bridge_provider_ids)
        sess_state: Dict[str, Dict[str, Any]] = {}
        for sid_raw, ca_raw, m_raw, ep_raw, inp_raw, out_raw in rows:
            sid = str(sid_raw or "")
            pid = sess_pid.get(sid)
            m_str = str(m_raw or "")
            if pid in excluded_set:
                continue
            if bridge_set and pid is not None and pid not in bridge_set and not m_str.lower().startswith("gemini-enterprise"):
                continue
            s_ca = str(ca_raw or "").strip()
            if not s_ca:
                continue
            raw_dt = datetime.fromisoformat((s_ca.replace("Z", "+00:00") if "T" in s_ca else s_ca + "+00:00"))
            dt = raw_dt.astimezone(b_tz)
            sh_dk = raw_dt.astimezone(sh_tz).strftime("%Y-%m-%d")
            dk = dt.strftime("%Y-%m-%d")
            ts = dt.timestamp()
            max_copilot_ts_by_day[dk] = max(max_copilot_ts_by_day.get(dk, 0.0), ts)
            p_tok = max(0, int(inp_raw or 0))
            c_tok = max(0, int(out_raw or 0))
            canon_m = resolve_canonical_billing_model(m_str)
            task_k = "responses" if str(ep_raw or "").strip() == "/responses" else "chat"
            prev = sess_state.get(sid)
            if (
                prev
                and prev["model"] == canon_m
                and p_tok >= prev["p"] * 0.85
                and prev["ctx"] < DEFAULT_CONTEXT_WINDOW
                and (ts - prev["ts"]) <= 1800
            ):
                eff_cached = min(p_tok, prev["ctx"])
                eff_delta = max(0, p_tok - eff_cached)
            else:
                eff_cached = 0
                eff_delta = p_tok
            sess_state[sid] = {"p": p_tok, "ctx": p_tok + c_tok, "ts": ts, "model": canon_m}
            rate = get_model_pricing(canon_m, prompt_tokens=p_tok, overrides=price_overrides)
            uncached_usd = (p_tok / 1_000_000.0) * rate.input_per_1m + (c_tok / 1_000_000.0) * rate.output_per_1m
            cached_usd = (
                (eff_cached / 1_000_000.0) * rate.cached_input_per_1m
                + (eff_delta / 1_000_000.0) * rate.input_per_1m
                + (c_tok / 1_000_000.0) * rate.output_per_1m
            )
            delta_b = {
                "requests": 1,
                "prompt_tokens": p_tok,
                "cached_tokens": eff_cached,
                "delta_prompt_tokens": eff_delta,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
                "images_generated": 0,
                "videos_generated": 0,
                "audio_requests": 0,
                "compactions": 0,
                "equivalent_cost_usd": round(uncached_usd, 8),
                "cached_equivalent_cost_usd": round(cached_usd, 8),
            }
            ev_grp = {
                "summary": delta_b,
                "by_model": {canon_m: dict(delta_b)},
                "by_task": {task_k: dict(delta_b)},
                "by_tenant": {
                    "host": {
                        "tenant_name": "本机直连",
                        "errors": 0,
                        "summary": dict(delta_b),
                        "by_model": {canon_m: dict(delta_b)},
                        "by_task": {task_k: dict(delta_b)},
                    }
                },
            }
            merge_group_into(rebuilt_days.setdefault(dk, empty_group()), ev_grp)
            merge_group_into(copilot_shanghai_days.setdefault(sh_dk, empty_group()), ev_grp)

    # Preserve non-Copilot residual usage & special media counts from legacy Shanghai day buckets
    for old_dk, old_dg_raw in old_days.items():
        if not isinstance(old_dg_raw, dict):
            continue
        old_dg = sanitize_group(old_dg_raw)
        cp_sh_dg = copilot_shanghai_days.get(str(old_dk)) or empty_group()
        rem_sum = _subtract_bucket(old_dg["summary"], cp_sh_dg["summary"])
        has_extra = any(int(rem_sum.get(k) or 0) > 0 for k in _BUCKET_INT_KEYS)
        if has_extra:
            rem_grp = empty_group()
            rem_grp["summary"] = rem_sum
            for mk, mb in old_dg["by_model"].items():
                rb = _subtract_bucket(mb, cp_sh_dg["by_model"].get(mk) or empty_bucket())
                if any(int(rb.get(k) or 0) > 0 for k in _BUCKET_INT_KEYS):
                    rem_grp["by_model"][mk] = rb
            for tk, tb in old_dg["by_task"].items():
                rb = _subtract_bucket(tb, cp_sh_dg["by_task"].get(tk) or empty_bucket())
                if any(int(rb.get(k) or 0) > 0 for k in _BUCKET_INT_KEYS):
                    rem_grp["by_task"][tk] = rb
            for tid, t_grp in old_dg["by_tenant"].items():
                cp_t = cp_sh_dg["by_tenant"].get(tid) or empty_tenant_group(t_grp.get("tenant_name") or tid)
                rt_sum = _subtract_bucket(t_grp["summary"], cp_t["summary"])
                if any(int(rt_sum.get(k) or 0) > 0 for k in _BUCKET_INT_KEYS):
                    nt = empty_tenant_group(t_grp.get("tenant_name") or tid)
                    nt["summary"] = rt_sum
                    rem_grp["by_tenant"][tid] = nt
            reconcile_unattributed_host_tenant(rem_grp)
            merge_group_into(rebuilt_days.setdefault(str(old_dk), empty_group()), rem_grp)

    # Normalize recent_requests day keys and fold error counts into rebuilt_days
    normalized_recents: List[Dict[str, Any]] = []
    for rec in old_recents:
        if not isinstance(rec, dict):
            continue
        item = dict(rec)
        ts_val = float(item.get("ts") or 0)
        if ts_val > 0:
            item["day"] = billing_day_key(ts_val, DEFAULT_BILLING_TIMEZONE)
        b_dk = str(item.get("day") or "")
        if b_dk and str(item.get("status") or "ok") != "ok":
            tid = str(item.get("tenant_id") or "host")
            tname = str(item.get("tenant_name") or "本机直连")
            dg = rebuilt_days.setdefault(b_dk, empty_group())
            tg = dg.setdefault("by_tenant", {}).setdefault(tid, empty_tenant_group(tname))
            tg["errors"] = int(tg.get("errors") or 0) + 1
        normalized_recents.append(item)

    sidecar_path = l_path.parent / "usage_backfill.json"
    sidecar_days = {k: rebuilt_days[k] for k in sorted(rebuilt_days.keys()) if k < cutoff_day}
    atomic_write_json(
        sidecar_path,
        {
            "version": LEDGER_SCHEMA_VERSION,
            "billing_timezone": DEFAULT_BILLING_TIMEZONE,
            "generated_at": int(time.time()),
            "cutoff_day": cutoff_day,
            "days": sidecar_days,
        },
        mode=0o600,
        dir_mode=0o755,
    )

    new_payload: Dict[str, Any] = {
        "version": LEDGER_SCHEMA_VERSION,
        "billing_timezone": DEFAULT_BILLING_TIMEZONE,
        "updated_at": int(time.time()),
        "archived": sanitize_group(old_raw.get("archived") if isinstance(old_raw, dict) else None),
        "days": {k: rebuilt_days[k] for k in sorted(rebuilt_days.keys())},
        "recent_requests": normalized_recents[-MAX_RECENT_REQUESTS:],
    }
    if isinstance(quota_cfg, dict):
        new_payload["quota_config"] = quota_cfg
    bak_path = l_path.parent / "usage_ledger.v1.bak.json"
    if l_path.is_file() and not bak_path.exists():
        atomic_write_json(bak_path, old_raw, mode=0o600, dir_mode=0o755)
    atomic_write_json(l_path, new_payload, mode=0o600, dir_mode=0o755)

    reloaded = UsageLedger(storage_path=l_path, price_overrides=price_overrides)
    snap = reloaded.snapshot()
    return {
        "migrated_days": sorted(rebuilt_days.keys()),
        "today_billing_day": snap["today_date"],
        "today_requests": snap["today"]["requests"],
        "cohort_quota": snap["cohort_quota"],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UsageLedger maintenance & historical backfill")
    parser.add_argument("--backfill", action="store_true", help="Backtest and merge pre-update usage history")
    parser.add_argument(
        "--migrate-billing-day",
        action="store_true",
        help="Re-bucket ledger into 00:00 America/Los_Angeles (16:00 Shanghai) billing days",
    )
    parser.add_argument(
        "--ledger",
        default=str(Path(__file__).resolve().parent / ".cache" / "usage_ledger.json"),
        help="Path to usage_ledger.json",
    )
    args = parser.parse_args()
    if args.migrate_billing_day:
        res = migrate_ledger_to_billing_days(args.ledger)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.backfill:
        res = backfill_pre_update_usage(args.ledger)
        print(json.dumps(res, ensure_ascii=False, indent=2))

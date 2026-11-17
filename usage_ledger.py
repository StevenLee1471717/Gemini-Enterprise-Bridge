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
from zoneinfo import ZoneInfo

from schemas import resolve_target_web_model

logger = logging.getLogger("gemini_bridge.ledger")

DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_COMPACT_BUDGET = 300_000
DEFAULT_USD_TO_CNY = 6.8
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_IMAGE_UNIT_USD = 0.04
DEFAULT_VIDEO_UNIT_USD = 0.35
MAX_RECENT_REQUESTS = 30
MAX_STORED_DAYS = 180

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
    """Resolve requested OpenAI model + live browser model into a canonical billing key."""
    req_target = resolve_target_web_model(requested_model)
    if req_target in _WEB_KEY_TO_CANONICAL:
        return _WEB_KEY_TO_CANONICAL[req_target]

    from_browser = _match_label_to_canonical(browser_active_model)
    if from_browser:
        return from_browser

    if requested_model and "gcloud" in requested_model.lower():
        from_gcloud = _match_label_to_canonical(gcloud_fallback_model)
        return from_gcloud or "gemini-2.5-pro"

    from_fuzzy = _match_label_to_canonical(requested_model)
    if from_fuzzy:
        return from_fuzzy

    return "gemini-enterprise-auto"


def get_model_pricing(
    canonical_model: str,
    prompt_tokens: int = 0,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ModelPricingRate:
    """Return reference Google Gemini API pricing (USD per 1M tokens) with optional overrides."""
    key = (canonical_model or "").strip().lower()
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
        "gemini-4-argon",
        "gemini-3.1-pro",
        "gemini-3.8-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-enterprise-auto",
    ]
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
    }


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
    return g


def merge_group_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    merge_bucket_into(dst["summary"], src["summary"])
    for mk, mb in (src.get("by_model") or {}).items():
        target_m = dst["by_model"].setdefault(mk, empty_bucket())
        merge_bucket_into(target_m, mb)
    for tk, tb in (src.get("by_task") or {}).items():
        target_t = dst["by_task"].setdefault(tk, empty_bucket())
        merge_bucket_into(target_t, tb)


def enrich_bucket(bucket: Dict[str, Any], usd_to_cny: float) -> Dict[str, Any]:
    uncached_usd = float(bucket.get("equivalent_cost_usd") or 0.0)
    cached_usd = float(bucket.get("cached_equivalent_cost_usd") or 0.0)
    saved_usd = max(0.0, uncached_usd - cached_usd)
    p_tok = int(bucket.get("prompt_tokens") or 0)
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
        }
    )
    return out


class UsageLedger:
    """Thread-safe persistent ledger with day-sourced double-entry consistency."""

    def __init__(
        self,
        storage_path: Optional[Union[Path, str]] = None,
        usd_to_cny_rate: float = DEFAULT_USD_TO_CNY,
        timezone_name: str = DEFAULT_TIMEZONE,
        price_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        image_unit_usd: float = DEFAULT_IMAGE_UNIT_USD,
        video_unit_usd: float = DEFAULT_VIDEO_UNIT_USD,
    ) -> None:
        self.storage_path: Optional[Path] = Path(storage_path) if storage_path else None
        self.usd_to_cny_rate = usd_to_cny_rate if usd_to_cny_rate > 0 else DEFAULT_USD_TO_CNY
        self.timezone_name = timezone_name or DEFAULT_TIMEZONE
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
        self._load_from_disk()

    def _day_key(self, as_of_date: Optional[Union[date, datetime, str]] = None) -> str:
        if isinstance(as_of_date, datetime):
            return as_of_date.date().isoformat()
        if isinstance(as_of_date, date):
            return as_of_date.isoformat()
        if isinstance(as_of_date, str) and as_of_date.strip():
            try:
                return date.fromisoformat(as_of_date.strip()[:10]).isoformat()
            except ValueError:
                pass
        try:
            return datetime.now(ZoneInfo(self.timezone_name)).date().isoformat()
        except Exception:
            return datetime.now().date().isoformat()

    def _load_from_disk(self) -> None:
        if self.storage_path is None or not self.storage_path.is_file():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            self._archived = sanitize_group(raw.get("archived"))
            if isinstance(raw.get("days"), dict):
                for dk, dv in raw["days"].items():
                    if isinstance(dv, dict):
                        self._days[str(dk)] = sanitize_group(dv)
            if isinstance(raw.get("recent_requests"), list):
                self._recent_requests = [
                    x for x in raw["recent_requests"][-MAX_RECENT_REQUESTS:] if isinstance(x, dict)
                ]
        except Exception as exc:
            logger.warning("读取本地账本 %s 失败（将重新初始化）: %s", self.storage_path, exc)

    def _save_to_disk_unlocked(self) -> None:
        if self.storage_path is None:
            return
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._days) > MAX_STORED_DAYS:
                sorted_keys = sorted(self._days.keys())
                to_archive = sorted_keys[:-MAX_STORED_DAYS]
                for old_k in to_archive:
                    old_group = self._days.pop(old_k, None)
                    if old_group:
                        merge_group_into(self._archived, old_group)
            payload = {
                "version": 1,
                "updated_at": int(time.time()),
                "archived": self._archived,
                "days": self._days,
                "recent_requests": self._recent_requests[-MAX_RECENT_REQUESTS:],
            }
            tmp_path = self.storage_path.with_name(self.storage_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.storage_path)
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
        day_key = self._day_key(as_of_date)
        now_ts = int(time.time())

        entry_delta: Dict[str, Any] = {
            "requests": 1,
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
        single_group = {
            "summary": entry_delta,
            "by_model": {canonical_model: dict(entry_delta)},
            "by_task": {norm_task: dict(entry_delta)},
        }

        record = {
            "ts": now_ts,
            "day": day_key,
            "slot_key": slot_key,
            "tab_id": tab_id,
            "tenant_id": (tenant_id or "host").strip() or "host",
            "tenant_name": (tenant_name or "本机直连").strip() or "本机直连",
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

        return {
            "summary": enrich_bucket(group.get("summary") or empty_bucket(), self.usd_to_cny_rate),
            "by_model": by_model_out,
            "by_task": by_task_out,
        }

    def snapshot(
        self,
        active_model_hint: Optional[str] = None,
        default_model: Optional[str] = None,
        as_of_date: Optional[Union[date, datetime, str]] = None,
        include_catalog: bool = False,
    ) -> Dict[str, Any]:
        """Build a consistent snapshot across all time windows, models, tasks, and daily series."""
        day_key = self._day_key(as_of_date)
        try:
            anchor_date = date.fromisoformat(day_key)
        except ValueError:
            anchor_date = datetime.now().date()
        yesterday_key = (anchor_date - timedelta(days=1)).isoformat()
        start_7d_key = (anchor_date - timedelta(days=6)).isoformat()
        start_30d_key = (anchor_date - timedelta(days=29)).isoformat()

        canonical_active = resolve_canonical_billing_model(
            default_model if not active_model_hint else None,
            active_model_hint,
        )
        active_rate = get_model_pricing(
            canonical_active, prompt_tokens=0, overrides=self.price_overrides
        )

        with self._lock:
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

            by_day_recent: Dict[str, Dict[str, Any]] = {}
            daily_details: Dict[str, Dict[str, Any]] = {}
            sorted_days_desc = sorted(self._days.keys(), reverse=True)
            for dk in sorted_days_desc[:14]:
                by_day_recent[dk] = enrich_bucket(self._days[dk]["summary"], self.usd_to_cny_rate)
            for dk in sorted_days_desc:
                daily_details[dk] = self._enrich_group(self._days[dk])

            result: Dict[str, Any] = {
                "token_count_source": "local_estimate_not_google_billing",
                "today_date": day_key,
                "yesterday_date": yesterday_key,
                "timezone": self.timezone_name,
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

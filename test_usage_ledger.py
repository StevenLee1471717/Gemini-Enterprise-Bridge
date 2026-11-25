"""Unit and end-to-end API tests for the local independent UsageLedger & context monitor."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import BridgeSettings
from server import create_app
from usage_ledger import UsageLedger, estimate_attachment_input_tokens, get_model_pricing

# Minimal valid 1x1 PNG and tiny MP4/WAV bytes for multimodal E2E tests
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_TINY_WAV_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)
_TINY_MP4_B64 = base64.b64encode(
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
).decode("ascii")


def test_ledger_time_windows_and_persistence(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_ledger.json"
    ledger = UsageLedger(storage_path=ledger_path, usd_to_cny_rate=7.0)

    # Older day (outside 7d, inside 30d): 1M input on Argon -> $2.00
    ledger.record_request(
        requested_model="gemini-4-argon",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        as_of_date="2026-10-25",
    )
    # Yesterday: 1M input, 0.1M output on Carbon -> $2.00 + $1.00 = $3.00
    ledger.record_request(
        requested_model="gemini-4-carbon",
        prompt_tokens=1_000_000,
        completion_tokens=100_000,
        as_of_date="2026-11-12",
    )
    # Day 2 (today): incremental continuation, 500k total prompt (400k cached, 100k delta), 50k output
    # Uncached = 0.5 * $2 + 0.05 * $10 = $1.50; Cached = 0.4 * $0.10 + 0.1 * $2 + 0.05 * $10 = $0.74
    ledger.record_request(
        requested_model="gemini-4-carbon",
        prompt_tokens=500_000,
        completion_tokens=50_000,
        strategy="smart_incremental_delta",
        cached_tokens=400_000,
        delta_prompt_tokens=100_000,
        as_of_date="2026-11-13",
    )

    snap = ledger.snapshot(as_of_date="2026-11-13")
    assert snap["all_time"]["requests"] == 3
    assert snap["all_time"]["equivalent_cost_usd"] == pytest.approx(6.50)
    assert snap["all_time"]["cached_equivalent_cost_usd"] == pytest.approx(5.74)
    assert snap["all_time"]["saved_cost_usd"] == pytest.approx(0.76)
    assert snap["today"]["requests"] == 1
    assert snap["today"]["equivalent_cost_usd"] == pytest.approx(1.50)
    assert snap["yesterday"]["requests"] == 1
    assert snap["yesterday"]["equivalent_cost_usd"] == pytest.approx(3.00)
    assert snap["last_7d"]["requests"] == 2
    assert snap["last_7d"]["equivalent_cost_usd"] == pytest.approx(4.50)
    assert set(snap["periods"]["7d"]["by_model"].keys()) == {"gemini-4-carbon"}
    assert snap["last_30d"]["requests"] == 3
    assert snap["last_30d"]["equivalent_cost_usd"] == pytest.approx(6.50)
    assert set(snap["periods"]["30d"]["by_model"].keys()) == {"gemini-4-carbon", "gemini-4-argon"}
    assert snap["daily"]["2026-11-12"]["summary"]["equivalent_cost_usd"] == pytest.approx(3.00)
    assert snap["current_session"]["requests"] == 3

    # Reload from disk and verify atomic persistence matches
    reloaded = UsageLedger(storage_path=ledger_path, usd_to_cny_rate=7.0)
    re_snap = reloaded.snapshot(as_of_date="2026-11-13")
    assert re_snap["all_time"]["requests"] == 3
    assert re_snap["all_time"]["equivalent_cost_usd"] == pytest.approx(6.50)
    assert re_snap["current_session"]["requests"] == 0
    assert not hasattr(reloaded, "reset")


def test_price_overrides_and_attachment_size_bytes() -> None:
    rate = get_model_pricing(
        "gemini-4-carbon",
        overrides={"gemini-4-carbon": {"input_per_1m": 3.5, "cached_input_per_1m": 0.35, "output_per_1m": 14.0}},
    )
    assert rate.input_per_1m == 3.5
    assert rate.cached_input_per_1m == 0.35
    assert rate.output_per_1m == 14.0

    # Verify collect_attachments' `size_bytes` key is honored
    toks = estimate_attachment_input_tokens([
        {"category": "image", "size_bytes": 1024},
        {"category": "document", "size_bytes": 360_000},
    ])
    assert toks == 258 + (360_000 // 1200)


def test_end_to_end_api_ledger_covers_all_task_types(tmp_path: Path) -> None:
    ledger_file = tmp_path / "e2e_ledger.json"
    settings = BridgeSettings(
        auto_export_extension=False,
        default_model="gemini-4-carbon",
        api_key="",
        generated_dir=str(tmp_path / "gen"),
        response_store_dir=str(tmp_path / "resp_store"),
        usage_ledger_path=str(ledger_file),
        usd_to_cny_rate=6.8,
    )
    app = create_app(settings)
    hub = app.state.hub

    with TestClient(app) as client:
        # Register a mock HTTP-polling browser worker tab
        reg = client.post(
            "/api/bridge/register",
            json={
                "client_id": "mock-ext",
                "tab_id": 701,
                "tab_url": "https://vertexaisearch.cloud.google.com/u/0/home",
                "tab_title": "Gemini Enterprise",
                "driver_mode": "auto",
                "active_model": "4-carbon",
            },
        )
        assert reg.status_code == 200

        orig_stream = hub.dispatch_stream_on_slot

        async def fake_stream_on_slot(*, slot, prompt, output_images=None, output_files=None, image_generation=False, video_generation=False, **kwargs):
            if image_generation:
                if output_images is not None:
                    output_images.append({"data_url": "data:image/png;base64," + _TINY_PNG_B64, "mime_type": "image/png"})
                yield "Generated image."
                return
            if video_generation:
                if output_files is not None:
                    output_files.append({"data_url": "data:video/mp4;base64," + _TINY_MP4_B64, "mime_type": "video/mp4", "name": "demo.mp4"})
                yield "Generated video."
                return
            yield "Reply OK for ledger test."

        hub.dispatch_stream_on_slot = fake_stream_on_slot
        try:
            # 1) Chat turn 1 + turn 2 (incremental delta)
            r1 = client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "sess-a"},
                json={"messages": [{"role": "user", "content": "Hello world turn one with context."}]},
            )
            assert r1.status_code == 200
            a1 = r1.json()["choices"][0]["message"]["content"]

            r2 = client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "sess-a"},
                json={
                    "messages": [
                        {"role": "user", "content": "Hello world turn one with context."},
                        {"role": "assistant", "content": a1},
                        {"role": "user", "content": "Short follow-up?"},
                    ]
                },
            )
            assert r2.status_code == 200

            mid_ctx = client.get("/api/status").json()["context_monitor"]
            assert mid_ctx["primary"]["turn_count"] == 4
            assert mid_ctx["primary"]["total_context_tokens"] > 0
            assert mid_ctx["primary"]["compact_threshold_tokens"] == 300_000
            assert mid_ctx["primary"]["remaining_to_compact_tokens"] == max(
                0, 300_000 - mid_ctx["primary"]["total_context_tokens"]
            )

            # 2) Codex /v1/responses (streaming)
            with client.stream(
                "POST",
                "/v1/responses",
                json={"input": "Run Codex task check", "stream": True},
            ) as rs:
                assert rs.status_code == 200
                body_txt = "".join(rs.iter_text())
                assert "response.completed" in body_txt

            # 3) Compaction (/v1/responses/compact)
            rc = client.post(
                "/v1/responses/compact",
                json={"input": [{"role": "user", "content": "Long conversation to compact"}]},
            )
            assert rc.status_code == 200

            # 4) Image generation & Image edit
            ri = client.post("/v1/images/generations", json={"prompt": "Draw a cat"})
            assert ri.status_code == 200
            re_img = client.post(
                "/v1/images/edits",
                json={"prompt": "Add a hat", "images": ["data:image/png;base64," + _TINY_PNG_B64]},
            )
            assert re_img.status_code == 200

            # 5) Video generation
            rv = client.post("/v1/videos/generations", json={"prompt": "Ocean waves"})
            assert rv.status_code == 200

            # 6) Audio transcription
            ra = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", _TINY_WAV_BYTES, "audio/wav")},
            )
            assert ra.status_code == 200

            # Verify /api/usage and /api/status
            u_res = client.get("/api/usage")
            assert u_res.status_code == 200
            u_data = u_res.json()["usage"]
            ctx_mon = u_res.json()["context_monitor"]

            assert u_data["all_time"]["requests"] == 8
            assert u_data["all_time"]["cached_tokens"] > 0
            assert u_data["all_time"]["images_generated"] == 2
            assert u_data["all_time"]["videos_generated"] == 1
            assert u_data["all_time"]["audio_requests"] == 1
            assert u_data["all_time"]["compactions"] == 1
            assert u_data["by_task"]["chat"]["requests"] == 2
            assert u_data["by_task"]["responses"]["requests"] == 1
            assert u_data["by_task"]["compaction"]["requests"] == 1
            assert u_data["by_task"]["image_gen"]["requests"] == 1
            assert u_data["by_task"]["image_edit"]["requests"] == 1
            assert u_data["by_task"]["video_gen"]["requests"] == 1
            assert u_data["by_task"]["audio"]["requests"] == 1
            assert u_data["periods"]["today"]["summary"]["requests"] == 8
            assert u_data["periods"]["today"]["by_task"]["compaction"]["requests"] == 1
            assert ctx_mon["primary"]["turn_count"] == 0

            # Verify reset endpoint & UI controls are permanently removed
            rst = client.post("/api/usage/reset", json={"scope": "today"})
            assert rst.status_code in (404, 405)
            dash_html = client.get("/").text
            assert "resetLedgerBtn" not in dash_html
            assert "524.3K" not in dash_html
            assert "300.0K" in dash_html
            assert "periodTabs" in dash_html
        finally:
            hub.dispatch_stream_on_slot = orig_stream


def test_pre_update_backfill_and_host_tenant_reconciliation(tmp_path: Path) -> None:
    import sqlite3
    from usage_ledger import backfill_pre_update_usage

    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir()
    ledger_path = cache_dir / "usage_ledger.json"

    # Pre-v1.5.0 day without by_tenant entry
    ledger_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": 1794900000,
                "archived": {},
                "days": {
                    "2026-11-17": {
                        "summary": {
                            "requests": 10,
                            "prompt_tokens": 1_000_000,
                            "cached_tokens": 800_000,
                            "delta_prompt_tokens": 200_000,
                            "completion_tokens": 50_000,
                            "total_tokens": 1_050_000,
                            "equivalent_cost_usd": 2.5,
                            "cached_equivalent_cost_usd": 0.98,
                        },
                        "by_model": {
                            "gemini-4-carbon": {
                                "requests": 10,
                                "prompt_tokens": 1_000_000,
                                "cached_tokens": 800_000,
                                "delta_prompt_tokens": 200_000,
                                "completion_tokens": 50_000,
                                "total_tokens": 1_050_000,
                                "equivalent_cost_usd": 2.5,
                                "cached_equivalent_cost_usd": 0.98,
                            }
                        },
                        "by_task": {
                            "chat": {
                                "requests": 10,
                                "prompt_tokens": 1_000_000,
                                "cached_tokens": 800_000,
                                "delta_prompt_tokens": 200_000,
                                "completion_tokens": 50_000,
                                "total_tokens": 1_050_000,
                                "equivalent_cost_usd": 2.5,
                                "cached_equivalent_cost_usd": 0.98,
                            }
                        },
                        "by_tenant": {},
                    }
                },
                "recent_requests": [],
            }
        ),
        encoding="utf-8",
    )

    # Legacy usage_stats.json (2026-11-16) + local Copilot BYOK SQLite (2026-11-11 & 2026-11-12)
    legacy_path = tmp_path / "usage_stats.json"
    legacy_path.write_text(
        json.dumps(
            {
                "by_day": {
                    "2026-11-16": {
                        "requests": 2,
                        "prompt_tokens": 300_000,
                        "cached_tokens": 200_000,
                        "delta_prompt_tokens": 100_000,
                        "completion_tokens": 10_000,
                        "total_tokens": 310_000,
                        "equivalent_cost_usd": 0.70,
                        "cached_equivalent_cost_usd": 0.32,
                    }
                },
                "by_model": {},
            }
        ),
        encoding="utf-8",
    )

    cp_dir = tmp_path / "copilot"
    cp_dir.mkdir()
    con_d = sqlite3.connect(str(cp_dir / "data.db"))
    con_d.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, provider_id TEXT)")
    con_d.execute("INSERT INTO sessions VALUES ('s-bridge', '3126a75e-77bb-4baa-b663-6abcc2cdb5a7')")
    con_d.execute("INSERT INTO sessions VALUES ('s-other', '695be4fc-bb13-4327-8c59-6bd91242b42d')")
    con_d.commit()
    con_d.close()

    con_s = sqlite3.connect(str(cp_dir / "session-store.db"))
    con_s.execute(
        "CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT, created_at TEXT, model TEXT, api_endpoint TEXT, input_tokens INTEGER, output_tokens INTEGER, token_details_json TEXT, total_nano_aiu INTEGER)"
    )
    con_s.execute(
        "INSERT INTO assistant_usage_events VALUES (1, 's-bridge', '2026-11-11T02:00:00Z', 'gemini-4-carbon', '/chat/completions', 100000, 5000, NULL, 0)"
    )
    con_s.execute(
        "INSERT INTO assistant_usage_events VALUES (2, 's-bridge', '2026-11-11T02:02:00Z', 'gemini-4-carbon', '/chat/completions', 120000, 5000, NULL, 0)"
    )
    # Non-bridge provider and native Copilot event must be ignored
    con_s.execute(
        "INSERT INTO assistant_usage_events VALUES (3, 's-other', '2026-11-11T02:03:00Z', 'gemini-4-carbon', '/chat/completions', 500000, 5000, NULL, 0)"
    )
    con_s.execute(
        "INSERT INTO assistant_usage_events VALUES (4, 's-bridge', '2026-11-11T02:04:00Z', 'gemini-4-carbon', '/chat/completions', 500000, 5000, '[{}]', 100)"
    )
    con_s.commit()
    con_s.close()

    res = backfill_pre_update_usage(
        ledger_path=ledger_path,
        legacy_stats_path=legacy_path,
        copilot_dir=cp_dir,
        cutoff_day="2026-11-17",
    )
    # 2026-11-11T02:00:00Z is 10:00 Shanghai (before 16:00 reset) -> 2026-11-10 18:00 PST billing day
    assert res["backfilled_days"] == ["2026-11-10", "2026-11-16"]

    ledger = UsageLedger(storage_path=ledger_path)
    snap = ledger.snapshot(as_of_date="2026-11-17")
    assert snap["all_time"]["requests"] == 14
    assert snap["by_tenant"]["host"]["summary"]["requests"] == 14
    assert snap["daily"]["2026-11-10"]["summary"]["cached_tokens"] == 105_000
    assert snap["daily"]["2026-11-16"]["summary"]["equivalent_cost_usd"] == pytest.approx(0.70)
    assert snap["daily"]["2026-11-16"]["summary"]["cached_equivalent_cost_usd"] == pytest.approx(0.32)


def test_pacific_billing_day_16pm_cutoff_and_cohort_quota(tmp_path: Path) -> None:
    from datetime import datetime
    from platform_compat import SHANGHAI_TZ
    from usage_ledger import compute_cohort_quota_metrics

    ledger_path = tmp_path / "quota_ledger.json"
    ledger = UsageLedger(
        storage_path=ledger_path,
        cohort_daily_quota=2000,
        cohort_reserved_quota=600,
    )

    # 2026-11-25 15:59:59+08:00 -> 2026-11-24 23:59:59 PST -> billing day 2026-11-24
    before_reset = datetime(2026, 11, 25, 15, 59, 59, tzinfo=SHANGHAI_TZ)
    rec_prev = ledger.record_request(
        requested_model="gemini-4-carbon",
        prompt_tokens=10_000,
        completion_tokens=1_000,
        as_of_date=before_reset,
    )
    assert rec_prev["day"] == "2026-11-24"

    # 2026-11-25 16:00:00+08:00 -> 2026-11-25 00:00:00 PST -> billing day 2026-11-25
    after_reset = datetime(2026, 11, 25, 16, 0, 0, tzinfo=SHANGHAI_TZ)
    for _ in range(7):
        rec_cur = ledger.record_request(
            requested_model="gemini-4-carbon",
            prompt_tokens=10_000,
            completion_tokens=1_000,
            as_of_date=after_reset,
        )
        assert rec_cur["day"] == "2026-11-25"

    snap = ledger.snapshot(as_of_date=after_reset)
    assert snap["today_date"] == "2026-11-25"
    assert snap["yesterday_date"] == "2026-11-24"
    assert snap["today"]["requests"] == 7
    assert snap["yesterday"]["requests"] == 1
    cq = snap["cohort_quota"]
    assert cq["cohort_daily_quota"] == 2000
    assert cq["cohort_reserved_quota"] == 600
    assert cq["usable_daily_budget"] == 1400
    assert cq["usable_used_pct"] == pytest.approx(0.5)
    assert cq["total_pool_used_pct"] == pytest.approx(0.35)
    assert cq["reserved_pool_pct"] == pytest.approx(30.0)
    assert cq["status_level"] == "ok"

    # Verify reserve consumption & upstream exhaustion metrics
    m_res = compute_cohort_quota_metrics(1470, 2000, 600)
    assert m_res["usable_used_pct"] == pytest.approx(105.0)
    assert m_res["reserve_consumed_requests"] == 70
    assert m_res["status_level"] == "reserve"

    ledger.record_failed_request(
        requested_model="gemini-4-carbon",
        status="rate_limited",
        status_code=429,
        error_code="quota_exceeded",
        error_message="Daily quota limit reached",
        as_of_date=after_reset,
    )
    snap_ex = ledger.snapshot(as_of_date=after_reset)
    assert snap_ex["cohort_quota"]["upstream_quota_exhausted_today"] is True
    assert snap_ex["cohort_quota"]["status_level"] == "exhausted"


def test_quota_config_api_and_dashboard_progress(tmp_path: Path) -> None:
    settings = BridgeSettings(
        api_key="",
        backend_mode="browser",
        generated_dir=str(tmp_path),
        usage_ledger_path=str(tmp_path / "usage_ledger.json"),
        auto_export_extension=False,
        auto_update=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        bad = client.post(
            "/api/usage/quota-config",
            json={"cohort_daily_quota": 1000, "cohort_reserved_quota": 1000},
        )
        assert bad.status_code == 400

        ok = client.post(
            "/api/usage/quota-config",
            json={"cohort_daily_quota": 1600, "cohort_reserved_quota": 500},
        )
        assert ok.status_code == 200
        cq = ok.json()["usage"]["cohort_quota"]
        assert cq["cohort_daily_quota"] == 1600
        assert cq["cohort_reserved_quota"] == 500
        assert cq["usable_daily_budget"] == 1100

        dash_html = client.get("/").text
        assert "cohortQuotaProgress" in dash_html
        assert "cohortUsablePctMain" in dash_html
        assert "cohortPoolSplitLabel" in dash_html
        assert "cohortBatchEfficiencyChip" in dash_html

    # Reload ledger from disk and verify quota_config + tool_calls batching metrics persisted
    reloaded = UsageLedger(storage_path=tmp_path / "usage_ledger.json")
    assert reloaded.cohort_daily_quota == 1600
    assert reloaded.cohort_reserved_quota == 500
    reloaded.record_request(
        requested_model="gemini-4-carbon",
        prompt_tokens=20_000,
        completion_tokens=4_000,
        tool_calls_count=5,
    )
    snap_eff = reloaded.snapshot()
    assert snap_eff["today"]["tool_calls"] == 5
    assert snap_eff["today"]["avg_tools_per_req"] == pytest.approx(5.0)
    assert snap_eff["today"]["avg_completion_tokens_per_req"] == pytest.approx(4000.0)

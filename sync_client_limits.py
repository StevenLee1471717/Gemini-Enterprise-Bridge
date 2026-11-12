"""Synchronize 1M context window & 65K output token limits into local BYOK client databases (e.g. GitHub Copilot App)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from schemas import DEFAULT_CONTEXT_WINDOW_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS


def default_copilot_db_path() -> Path:
    env_override = os.getenv("COPILOT_DATA_DB", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    return Path.home() / ".copilot" / "data.db"


def _is_local_bridge_provider(
    row: sqlite3.Row,
    provider_name: str = "gemini-enterprise",
    bridge_port: int = 8000,
) -> bool:
    name = str(row["name"] or "").strip().lower()
    ptype = str(row["type"] or "").strip().lower()
    if ptype and ptype != "custom":
        return False
    if provider_name and name == provider_name.strip().lower():
        return True

    raw_settings = str(row["settings_json"] or "").strip()
    if not raw_settings:
        return False
    try:
        parsed = json.loads(raw_settings)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    base_url = str(parsed.get("baseUrl") or "").strip().lower().rstrip("/")
    local_prefixes = (
        f"http://127.0.0.1:{bridge_port}",
        f"http://localhost:{bridge_port}",
    )
    return any(base_url == p or base_url == f"{p}/v1" for p in local_prefixes)


def sync_copilot_provider_limits(
    db_path: Optional[Path] = None,
    provider_id: Optional[str] = None,
    provider_name: str = "gemini-enterprise",
    bridge_port: int = 8000,
    max_prompt_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    dry_run: bool = False,
    backup_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ensure BYOK models registered under Gemini Enterprise Bridge carry 1M prompt / 65K output limits.

    GitHub Copilot App defaults `provider_models.max_prompt_tokens IS NULL` to a 128,000-token fallback.
    Updating `max_prompt_tokens` and `max_output_tokens` in SQLite (`busy_timeout = 5000`) lifts that cap.
    """
    target_db = Path(db_path) if db_path is not None else default_copilot_db_path()
    if not target_db.is_file():
        return {
            "ok": True,
            "skipped": True,
            "reason": "db_not_found",
            "db_path": str(target_db),
            "providers_matched": [],
            "models_checked": 0,
            "models_updated": [],
            "dry_run": dry_run,
        }

    conn = sqlite3.connect(str(target_db), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        tables = {
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('model_providers', 'provider_models')"
            ).fetchall()
        }
        if not {"model_providers", "provider_models"}.issubset(tables):
            return {
                "ok": True,
                "skipped": True,
                "reason": "provider_tables_absent",
                "db_path": str(target_db),
                "providers_matched": [],
                "models_checked": 0,
                "models_updated": [],
                "dry_run": dry_run,
            }

        if provider_id:
            provider_rows = conn.execute(
                "SELECT id, name, type, settings_json FROM model_providers WHERE id = ?",
                (provider_id.strip(),),
            ).fetchall()
        else:
            all_providers = conn.execute(
                "SELECT id, name, type, settings_json FROM model_providers"
            ).fetchall()
            provider_rows = [
                r
                for r in all_providers
                if _is_local_bridge_provider(
                    r, provider_name=provider_name, bridge_port=bridge_port
                )
            ]

        matched_providers = [
            {"id": str(r["id"]), "name": str(r["name"])} for r in provider_rows
        ]
        if not matched_providers:
            return {
                "ok": True,
                "skipped": False,
                "db_path": str(target_db),
                "providers_matched": [],
                "models_checked": 0,
                "models_updated": [],
                "dry_run": dry_run,
            }

        models_checked = 0
        candidates: List[Dict[str, Any]] = []
        for prov in matched_providers:
            rows = conn.execute(
                """
                SELECT id, provider_id, model_id, display_name, max_prompt_tokens, max_output_tokens
                  FROM provider_models
                 WHERE provider_id = ?
                """,
                (prov["id"],),
            ).fetchall()
            models_checked += len(rows)
            for m in rows:
                cur_prompt = m["max_prompt_tokens"]
                cur_output = m["max_output_tokens"]
                needs_prompt = cur_prompt is None or int(cur_prompt) < max_prompt_tokens
                needs_output = cur_output is None or int(cur_output) < max_output_tokens
                if needs_prompt or needs_output:
                    candidates.append(
                        {
                            "id": str(m["id"]),
                            "provider_id": str(m["provider_id"]),
                            "model_id": str(m["model_id"]),
                            "old_max_prompt_tokens": cur_prompt,
                            "old_max_output_tokens": cur_output,
                            "new_max_prompt_tokens": max_prompt_tokens,
                            "new_max_output_tokens": max_output_tokens,
                        }
                    )

        if candidates and not dry_run:
            if backup_path is not None:
                backup_file = Path(backup_path)
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                bck_conn = sqlite3.connect(str(backup_file), timeout=5.0)
                try:
                    conn.backup(bck_conn)
                finally:
                    bck_conn.close()

            with conn:
                for prov in matched_providers:
                    conn.execute(
                        """
                        UPDATE provider_models
                           SET max_prompt_tokens = ?,
                               max_output_tokens = ?,
                               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                         WHERE provider_id = ?
                           AND (
                               max_prompt_tokens IS NULL
                               OR max_prompt_tokens < ?
                               OR max_output_tokens IS NULL
                               OR max_output_tokens < ?
                           )
                        """,
                        (
                            max_prompt_tokens,
                            max_output_tokens,
                            prov["id"],
                            max_prompt_tokens,
                            max_output_tokens,
                        ),
                    )

        return {
            "ok": True,
            "skipped": False,
            "db_path": str(target_db),
            "providers_matched": matched_providers,
            "models_checked": models_checked,
            "models_updated": candidates,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync 1,048,576 prompt / 65,536 output token limits for Gemini Enterprise Bridge in Copilot App SQLite DB"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Explicit path to Copilot data.db (defaults to ~/.copilot/data.db)",
    )
    parser.add_argument(
        "--provider-id",
        type=str,
        default=None,
        help="Optional explicit provider_id UUID in model_providers",
    )
    parser.add_argument(
        "--provider-name",
        type=str,
        default="gemini-enterprise",
        help="Custom provider name to match (default: gemini-enterprise)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BRIDGE_PORT", "8000") or "8000"),
        help="Local bridge port used to match custom provider baseUrl (default: 8000)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run mode: inspect and print models needing limit updates without modifying data.db",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="Optional path to write an online SQLite backup before updating rows",
    )
    args = parser.parse_args()
    result = sync_copilot_provider_limits(
        db_path=args.db_path,
        provider_id=args.provider_id,
        provider_name=args.provider_name,
        bridge_port=args.port,
        dry_run=args.check,
        backup_path=args.backup,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()

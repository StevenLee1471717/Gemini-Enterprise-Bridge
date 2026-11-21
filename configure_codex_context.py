"""Synchronize local Gemini Codex budgets and tools without editing other providers.

Run without --apply to preview. Backups are required before changing local files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tomllib

from agent_policy import (
    CARBON_CONTEXT_WINDOW, CARBON_AUTO_COMPACT_TOKENS,
    CARBON_EFFECTIVE_CONTEXT_PERCENT, CODING_WORKFLOW,
    GEMINI_DESKTOP_BASE_INSTRUCTIONS, upgrade_desktop_agent_instructions,
)
from platform_compat import best_effort_chmod, reconfigure_stdio_utf8, sqlite_file_uri


def _safe_backup_relpath(path: Path) -> Path:
    """Convert an absolute POSIX or Windows drive/UNC path into a safe relative subpath."""
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lstrip("\\/").replace("\\", "/")
    parts = [p for p in resolved.parts if p not in {resolved.anchor, "/", "\\", ""}]
    if drive:
        return Path(drive, *parts)
    return Path(*parts) if parts else Path(resolved.name)


def render_local_bridge_profile(
    repo_dir: Path,
    python_executable: str | Path | None = None,
    port: int = 8000,
) -> str:
    """Render a cross-platform `gemini-bridge.config.toml` with properly escaped paths."""
    repo_resolved = repo_dir.resolve()
    py_exec = str(Path(python_executable).resolve()) if python_executable else str(Path(sys.executable).resolve())
    catalog_path = str(repo_resolved / "codex-models.json")
    mcp_script = str(repo_resolved / "imagegen_mcp.py")
    return (
        "# Gemini Enterprise Bridge - dedicated Codex profile\n"
        'model = "gemini-4-carbon"\n'
        'model_provider = "gemini_bridge"\n'
        'model_reasoning_effort = "none"\n'
        'model_reasoning_summary = "none"\n'
        f"model_context_window = {CARBON_CONTEXT_WINDOW}\n"
        f"model_auto_compact_token_limit = {CARBON_AUTO_COMPACT_TOKENS}\n"
        'web_search = "disabled"\n'
        f"model_catalog_json = {json.dumps(catalog_path, ensure_ascii=False)}\n\n"
        "[model_providers.gemini_bridge]\n"
        'name = "Gemini Enterprise Bridge"\n'
        f'base_url = "http://127.0.0.1:{int(port)}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
        "request_max_retries = 1\n"
        "stream_max_retries = 1\n"
        "stream_idle_timeout_ms = 660000\n\n"
        "[mcp_servers.gemini_bridge_images]\n"
        f"command = {json.dumps(py_exec, ensure_ascii=False)}\n"
        f"args = [{json.dumps(mcp_script, ensure_ascii=False)}]\n"
        "tool_timeout_sec = 1250\n"
        'default_tools_approval_mode = "approve"\n\n'
        "# Local multimodal bridge tools are pre-approved without per-call confirmation.\n"
        "[mcp_servers.gemini_bridge_images.tools.generate_image]\n"
        'approval_mode = "approve"\n\n'
        "[mcp_servers.gemini_bridge_images.tools.generate_video]\n"
        'approval_mode = "approve"\n\n'
        "[mcp_servers.gemini_bridge_images.tools.analyze_media]\n"
        'approval_mode = "approve"\n'
    )


def update_catalog(text: str) -> str:
    document = json.loads(text)
    changed = False
    for model in document.get('models', []):
        slug = str(model.get('slug') or '')
        if not slug.startswith('gemini-'):
            continue
        model.update(context_window=CARBON_CONTEXT_WINDOW, max_context_window=CARBON_CONTEXT_WINDOW,
                     auto_compact_token_limit=CARBON_AUTO_COMPACT_TOKENS,
                     effective_context_window_percent=CARBON_EFFECTIVE_CONTEXT_PERCENT)
        # Codex limits a single tool response; keep bounded, but permit useful source ranges.
        model['truncation_policy'] = {'mode': 'tokens', 'limit': 10000}
        model['apply_patch_tool_type'] = 'freeform'
        modalities = list(model.get('input_modalities') or ['text'])
        for required in ('text', 'image'):
            if required not in modalities:
                modalities.append(required)
        model['input_modalities'] = modalities
        if not isinstance(model.get('base_instructions'), str) or not model['base_instructions'].strip():
            model['base_instructions'] = GEMINI_DESKTOP_BASE_INSTRUCTIONS
        mm = model.get('model_messages')
        if isinstance(mm, dict):
            for incompatible_key in ('persistent_instructions', 'token_budget', 'guardian_v2', 'confirmation_policies'):
                mm.pop(incompatible_key, None)
            if isinstance(mm.get('multi_agent'), dict):
                mm['multi_agent'] = None
            if not isinstance(mm.get('instructions_template'), str) or not mm['instructions_template'].strip():
                mm['instructions_template'] = GEMINI_DESKTOP_BASE_INSTRUCTIONS
        for container, key in [(model, 'base_instructions'), (model.get('model_messages') or {}, 'instructions_template')]:
            if isinstance(container.get(key), str):
                old = re.sub(r'\n*\[Coding workflow\].*?\[/Coding workflow\]\n*', '', container[key], flags=re.S)
                old = upgrade_desktop_agent_instructions(old)
                container[key] = old.rstrip() + '\n\n' + CODING_WORKFLOW
        changed = True
    return json.dumps(document, ensure_ascii=False, indent=2) + '\n' if changed else text


def update_stream_timeouts(text: str) -> str:
    document = tomllib.loads(text)
    for name, provider in document.get('model_providers', {}).items():
        if provider.get('base_url', '').rstrip('/') not in {
            'http://127.0.0.1:8000/v1', 'http://127.0.0.1:18001/v1',
        }:
            continue
        table = re.search(r'^\[model_providers\.(?:' + re.escape(name) + '|' + re.escape(json.dumps(name)) + r')\][^\n]*\n', text, re.M)
        if table is None:
            continue
        following = re.search(r'^\[', text[table.end():], re.M)
        end = table.end() + following.start() if following else len(text)
        body = text[table.end():end]
        pattern = r'^stream_idle_timeout_ms\s*=.*$'
        if re.search(pattern, body, re.M):
            body = re.sub(pattern, 'stream_idle_timeout_ms = 660000', body, flags=re.M)
        else:
            body = 'stream_idle_timeout_ms = 660000\n' + body
        text = text[:table.end()] + body + text[end:]
    tomllib.loads(text)
    return text


def update_profile(text: str) -> str:
    document = tomllib.loads(text)
    if not str(document.get('model') or '').startswith('gemini-'):
        return text
    # Restrict replacements to the root table, preserving providers, plugins, and unrelated preferences.
    split = re.search(r'^\[', text, re.M)
    head, tail = (text[:split.start()], text[split.start():]) if split else (text, '')
    for key, value in [('model_context_window', CARBON_CONTEXT_WINDOW), ('model_auto_compact_token_limit', CARBON_AUTO_COMPACT_TOKENS)]:
        pattern = rf'^{key}\s*=.*$'
        if re.search(pattern, head, re.M):
            head = re.sub(pattern, f'{key} = {value}', head, flags=re.M)
        else:
            head = f'{key} = {value}\n' + head
    result = head + tail
    tomllib.loads(result)
    return update_stream_timeouts(result)


def update_ccswitch(database: Path, apply: bool, backup_dir: Path | None) -> list[str]:
    if not database.is_file():
        return []
    connection = sqlite3.connect(sqlite_file_uri(database, 'rw' if apply else 'ro'), uri=True, timeout=3)
    try:
        changes = []
        for identifier, raw in connection.execute("SELECT id,settings_config FROM providers WHERE app_type='codex'").fetchall():
            document = json.loads(raw)
            config = document.get('config')
            if not isinstance(config, str) or '127.0.0.1:8000' not in config:
                continue
            updated = update_profile(config)
            if updated != config:
                document['config'] = updated
                changes.append((identifier, raw, json.dumps(document, ensure_ascii=False)))
        if apply and changes:
            backup = backup_dir / 'cc-switch.db'
            if backup.exists():
                raise RuntimeError(f'Backup already exists: {backup}')
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup) as destination:
                connection.backup(destination)
            best_effort_chmod(backup, 0o600)
            with connection:
                for identifier, raw, updated in changes:
                    count = connection.execute("UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex' AND settings_config=?", (updated, identifier, raw)).rowcount
                    if count != 1:
                        raise RuntimeError('CC Switch configuration changed concurrently')
        return ['CC Switch: ' + identifier for identifier, _, _ in changes]
    finally:
        connection.close()

def main():
    reconfigure_stdio_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--outputs', type=Path, default=None)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--ensure-bridge-profile', action='store_true',
                        help='Create or update <codex-home>/gemini-bridge.config.toml with current OS paths.')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error('--apply requires --backup-dir')
    repo_dir = Path(__file__).resolve().parent
    catalogs = [repo_dir / 'codex-models.json', args.codex_home / 'model-router/models.json',
                args.codex_home / 'gemini-ccswitch-models.json']
    profiles = [args.codex_home / 'gemini-bridge.config.toml', args.codex_home / 'gemini-4-carbon.config.toml',
                args.codex_home / 'gemini-4-argon.config.toml']
    if args.outputs is not None:
        profiles.extend([args.outputs / 'gemini-bridge.config.toml', args.outputs / 'ccswitch-gemini.config.toml'])
    # The shared desktop config switches providers; per-model catalogs own its budgets.
    if args.apply:
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        best_effort_chmod(args.backup_dir, 0o700)
    changes = []
    if args.ensure_bridge_profile:
        bridge_prof = args.codex_home / 'gemini-bridge.config.toml'
        desired_prof = render_local_bridge_profile(repo_dir, sys.executable, port=args.port)
        existing_prof = bridge_prof.read_text(encoding='utf-8') if bridge_prof.is_file() else None
        if existing_prof != desired_prof:
            if args.apply:
                args.codex_home.mkdir(parents=True, exist_ok=True)
                if existing_prof is not None:
                    backup = args.backup_dir / _safe_backup_relpath(bridge_prof)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if not backup.exists():
                        shutil.copy2(bridge_prof, backup)
                bridge_prof.write_text(desired_prof, encoding='utf-8')
            changes.append(str(bridge_prof))
    targets = ([(p, update_catalog) for p in catalogs] + [(p, update_profile) for p in profiles]
               + [(args.codex_home / 'config.toml', update_stream_timeouts)])
    for path, transform in targets:
        if not path.is_file():
            continue
        original = path.read_text(encoding='utf-8')
        updated = transform(original)
        if original == updated:
            continue
        if args.apply:
            # Re-read before replacing so a concurrent configuration edit is never overwritten.
            if path.read_text(encoding='utf-8') != original:
                raise RuntimeError(f'File changed during configuration update: {path}')
            backup = args.backup_dir / _safe_backup_relpath(path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                raise RuntimeError(f'Backup already exists: {backup}')
            shutil.copy2(path, backup)
            temp = path.with_name(path.name + '.context-update.tmp')
            try:
                temp.write_text(updated, encoding='utf-8')
                shutil.copymode(path, temp)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        changes.append(str(path))
    ccswitch_candidates = [args.codex_home.parent / '.cc-switch/cc-switch.db']
    appdata = os.environ.get('APPDATA', '').strip()
    if appdata:
        ccswitch_candidates.append(Path(appdata) / 'cc-switch/cc-switch.db')
    seen_dbs: set[Path] = set()
    for db_cand in ccswitch_candidates:
        if db_cand.is_file() and db_cand.resolve() not in seen_dbs:
            seen_dbs.add(db_cand.resolve())
            changes.extend(update_ccswitch(db_cand, args.apply, args.backup_dir))
    print(json.dumps({'applied': args.apply, 'files': changes,
                      'carbon_context_window': CARBON_CONTEXT_WINDOW,
                      'checkpoint_tokens': CARBON_AUTO_COMPACT_TOKENS,
                      'checkpoint_basis': 'Local coding stability budget, not a published Enterprise web trigger'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

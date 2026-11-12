"""Synchronize local Gemini Codex budgets and tools without editing other providers.

Run without --apply to preview. Backups are required before changing local files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tomllib

from agent_policy import (
    CARBON_CONTEXT_WINDOW, CARBON_AUTO_COMPACT_TOKENS,
    CARBON_EFFECTIVE_CONTEXT_PERCENT, CODING_WORKFLOW,
)


def update_catalog(text: str) -> str:
    document = json.loads(text)
    changed = False
    for model in document.get('models', []):
        if model.get('slug') not in {'gemini-4-carbon', 'gemini-4-argon'}:
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
        for container, key in [(model, 'base_instructions'), (model.get('model_messages') or {}, 'instructions_template')]:
            if isinstance(container.get(key), str):
                old = re.sub(r'\n*\[Coding workflow\].*?\[/Coding workflow\]\n*', '', container[key], flags=re.S)
                container[key] = old.rstrip() + '\n\n' + CODING_WORKFLOW
        changed = True
    return json.dumps(document, ensure_ascii=False, indent=2) + '\n' if changed else text


def update_profile(text: str) -> str:
    document = tomllib.loads(text)
    if document.get('model') not in {'gemini-4-carbon', 'gemini-4-argon'}:
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
    return result


def update_ccswitch(database: Path, apply: bool, backup_dir: Path | None) -> list[str]:
    if not database.is_file():
        return []
    connection = sqlite3.connect('file:' + str(database) + '?mode=' + ('rw' if apply else 'ro'), uri=True, timeout=3)
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
            backup.chmod(0o600)
            with connection:
                for identifier, raw, updated in changes:
                    count = connection.execute("UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex' AND settings_config=?", (updated, identifier, raw)).rowcount
                    if count != 1:
                        raise RuntimeError('CC Switch configuration changed concurrently')
        return ['CC Switch: ' + identifier for identifier, _, _ in changes]
    finally:
        connection.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--outputs', type=Path, default=None)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error('--apply requires --backup-dir')
    catalogs = [Path(__file__).with_name('codex-models.json'), args.codex_home / 'model-router/models.json',
                args.codex_home / 'gemini-ccswitch-models.json']
    profiles = [args.codex_home / 'gemini-bridge.config.toml', args.codex_home / 'gemini-4-carbon.config.toml',
                args.codex_home / 'gemini-4-argon.config.toml']
    if args.outputs is not None:
        profiles.extend([args.outputs / 'gemini-bridge.config.toml', args.outputs / 'ccswitch-gemini.config.toml'])
    # The shared desktop config switches providers; per-model catalogs own its budgets.
    if args.apply:
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        args.backup_dir.chmod(0o700)
    changes = []
    for path, transform in [(p, update_catalog) for p in catalogs] + [(p, update_profile) for p in profiles]:
        if not path.is_file():
            continue
        original = path.read_text()
        updated = transform(original)
        if original == updated:
            continue
        if args.apply:
            # Re-read before replacing so a concurrent configuration edit is never overwritten.
            if path.read_text() != original:
                raise RuntimeError(f'File changed during configuration update: {path}')
            backup = args.backup_dir / (str(path).lstrip('/'))
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                raise RuntimeError(f'Backup already exists: {backup}')
            shutil.copy2(path, backup)
            temp = path.with_name(path.name + '.context-update.tmp')
            try:
                temp.write_text(updated)
                shutil.copymode(path, temp)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        changes.append(str(path))
    changes.extend(update_ccswitch(args.codex_home.parent / '.cc-switch/cc-switch.db', args.apply, args.backup_dir))
    print(json.dumps({'applied': args.apply, 'files': changes,
                      'carbon_context_window': CARBON_CONTEXT_WINDOW,
                      'checkpoint_tokens': CARBON_AUTO_COMPACT_TOKENS,
                      'checkpoint_basis': 'Google Gemini CLI 50%, not a published Enterprise web trigger'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

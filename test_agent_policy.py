"""Regression checks for file fidelity, continuation, and Carbon-only configuration."""
import copy
import json
import tomllib

from agent_policy import (
    CARBON_CONTEXT_WINDOW, CARBON_AUTO_COMPACT_TOKENS,
    deduplicate_tool_text, reading_diagnostics, reading_reminder,
    split_checkpoint_history,
)
from configure_codex_context import update_catalog, update_profile
from context_builder import ConversationContextBuilder
from schemas import ChatMessage
from test_responses import browser
from tool_calling import build_tool_system_instructions


def shell_tool():
    return {'type': 'function', 'function': {'name': 'functions.exec_command',
            'description': 'Run a local command.', 'parameters': {'type': 'object'}}}


def call(identifier, command):
    return ChatMessage(role='assistant', tool_calls=[{'id': identifier, 'type': 'function',
        'function': {'name': 'functions.exec_command', 'arguments': json.dumps({'cmd': command})}}])


def test_tool_rules_at_end_of_long_catalog_are_not_silently_deleted():
    rule = 'NEVER change system settings; use absolute paths; preserve newline characters.'
    tools = [shell_tool() for _ in range(20)]
    for i, tool in enumerate(tools):
        tool['function']['name'] += str(i)
        tool['function']['description'] = ('A detailed instruction. ' * 80) + rule
        tool['function']['parameters'] = {'type': 'object', 'properties': {
            'cmd': {'type': 'string', 'description': 'Explanation. ' * 60 + rule}},
            'required': ['cmd'], 'additionalProperties': False}
    original = copy.deepcopy(tools)
    rendered = build_tool_system_instructions(tools)
    specs = json.JSONDecoder().raw_decode(rendered[rendered.index('[{'):])[0]
    assert tools == original
    assert all(item['description'].endswith(rule) for item in specs)
    assert all(item['parameters']['properties']['cmd']['description'].endswith(rule) for item in specs)
    assert all(item['parameters']['additionalProperties'] is False for item in specs)


def test_exact_large_duplicate_references_first_result_but_changed_output_survives():
    seen = {}
    body = 'original source\n' * 600
    one = 'Chunk ID: one\nWall time: 1\nProcess exited with code 0\nOutput:\n' + body
    two = one.replace('Chunk ID: one', 'Chunk ID: two')
    assert deduplicate_tool_text(one, 'c1', seen) == one
    repeated = deduplicate_tool_text(two, 'c2', seen)
    assert 'call_id=c1' in repeated and 'Chunk ID: two' in repeated
    assert body not in repeated
    changed = two + 'CRITICAL new line'
    assert deduplicate_tool_text(changed, 'c3', seen) == changed
    failed = two.replace('code 0', 'code 1')
    assert deduplicate_tool_text(failed, 'c4', seen) == failed
    assert deduplicate_tool_text(body, '', {}) == body


def test_deduplication_does_not_break_incremental_history_or_hide_first_source():
    builder = ConversationContextBuilder()
    tools = [shell_tool()]
    body = 'line of exact source\n' * 400
    history = [ChatMessage(role='user', content='Inspect the source.'), call('c1', 'cat big.txt'),
               ChatMessage(role='tool', content=body, tool_call_id='c1')]
    first = builder.build(history, tools=tools)
    assert body in first.prompt
    builder.record_completed_turn(session_key="default", sent_history=first.normalized_history,
        assistant_tool_calls=call('c2', 'cat big.txt').tool_calls,
        tools_fingerprint=first.tools_fingerprint, system_fingerprint=first.system_fingerprint)
    history += [call('c2', 'cat big.txt'), ChatMessage(role='tool', content=body, tool_call_id='c2')]
    second = builder.build(history, tools=tools)
    assert second.strategy_used == 'smart_incremental_delta'
    assert second.reset_chat is False
    assert body not in second.prompt and 'call_id=c1' in second.prompt
    # A new web session is reconstructed with the full first output and a reference to it.
    packed = ConversationContextBuilder().build(history, tools=tools)
    assert packed.prompt.count(body) == 1 and 'call_id=c1' in packed.prompt
    assert history[-1].content == body


def test_truncation_and_repeated_reads_add_targeted_hint_without_removing_results():
    content = 'Warning: truncated output (original token count: 50000)\nEXACT EVIDENCE'
    history = [ChatMessage(role='user', content='continue')]
    for i in range(3):
        history += [call(str(i), 'cat source.py'), ChatMessage(role='tool', content=content, tool_call_id=str(i))]
    diagnostics = reading_diagnostics(history)
    assert diagnostics['latest_result_truncated'] and diagnostics['repeated_read_calls'] == 2
    assert 'missing relevant line range' in reading_reminder(diagnostics)
    prepared = ConversationContextBuilder().build(history, tools=[shell_tool()])
    assert 'EXACT EVIDENCE' in prepared.prompt
    assert '[Reading checkpoint]' in prepared.prompt
    assert 'Several recent read calls repeat' in prepared.prompt


def test_checkpoint_keeps_recent_tool_rounds_and_pending_call_ids_atomic():
    messages = [ChatMessage(role='developer', content='Preserve local changes'),
        ChatMessage(role='user', content='Fix the issue'), call('old', 'rg bug'),
        ChatMessage(role='tool', tool_call_id='old', content='located'),
        call('recent', 'sed -n 1,20p file.py'), ChatMessage(role='tool', tool_call_id='recent', content='recent source'),
        call('pending', 'pytest')]
    prefix, tail = split_checkpoint_history(messages)
    assert prefix[-1].tool_call_id == 'old'
    assert tail[0].tool_calls[0]['id'] == 'recent'
    assert tail[-1].tool_calls[0]['id'] == 'pending'
    assert messages[0].role == 'developer'


def test_usage_counts_tool_instructions_and_assistant_arguments_consistently():
    tools = [{'type': 'function', 'name': 'functions.exec_command', 'description': 'LONG TOOL RULE ' * 200,
              'parameters': {'type': 'object'}}]
    user = {'role': 'user', 'content': 'hello'}
    previous = {'type': 'function_call', 'name': 'functions.exec_command', 'call_id': 'c1',
                'arguments': json.dumps({'cmd': 'LONG ARGUMENT ' * 800})}
    with browser(['ok']) as (client, commands):
        small = client.post('/v1/responses/input_tokens', json={'input': [user]}).json()['input_tokens']
        payload = {'input': [user, previous, {'type': 'function_call_output', 'call_id': 'c1', 'output': 'done'}], 'tools': tools}
        large = client.post('/v1/responses/input_tokens', json=payload).json()['input_tokens']
        result = client.post('/v1/responses', json=payload).json()
        assert large > small + 2000
        assert result['usage']['input_tokens'] == large
        status = client.get('/api/status').json()['context']
        assert status['last_request']['estimated_logical_input_tokens'] == large
        assert status['token_count_source'] == 'local_estimate_not_google_usage'


def test_catalog_update_is_idempotent_and_leaves_other_models_unchanged():
    others = {'slug': 'gpt-6.1-sol', 'context_window': 500000, 'base_instructions': 'Original'}
    source = {'models': [others, {'slug': 'gemini-4-carbon', 'context_window': 32000,
              'base_instructions': 'Original', 'model_messages': {'instructions_template': 'Original'}}]}
    updated = update_catalog(json.dumps(source))
    document = json.loads(updated)
    assert document['models'][0] == others
    carbon = document['models'][1]
    assert carbon['context_window'] == CARBON_CONTEXT_WINDOW
    assert carbon['auto_compact_token_limit'] == CARBON_AUTO_COMPACT_TOKENS
    assert update_catalog(updated) == updated
    assert carbon['base_instructions'].count('[Coding workflow]') == 1


def test_profile_update_preserves_providers_plugins_and_non_carbon_profiles():
    source = 'model = "gemini-4-carbon"\nmodel_context_window = 32000\n[plugins.example]\nenabled = true\n'
    result = tomllib.loads(update_profile(source))
    assert result['model_context_window'] == CARBON_CONTEXT_WINDOW
    assert result['model_auto_compact_token_limit'] == CARBON_AUTO_COMPACT_TOKENS
    assert result['plugins']['example']['enabled'] is True
    assert update_profile(source.replace('gemini-4-carbon', 'gpt-6.1-sol')) == source.replace('gemini-4-carbon', 'gpt-6.1-sol')


def test_compaction_endpoint_replays_recent_evidence_and_unresolved_calls():
    items = [{'role': 'user', 'content': 'Fix a bug'}]
    for index in range(3):
        items.append({'type': 'function_call', 'name': 'read_file', 'call_id': f'c{index}', 'arguments': '{}'})
        items.append({'type': 'function_call_output', 'call_id': f'c{index}', 'output': f'RECENT_EVIDENCE_{index}'})
    with browser(['Goal: fix bug. Located the source.', 'continue']) as (client, commands):
        compact = client.post('/v1/responses/compact', json={'input': items}).json()
        assert 'RECENT_EVIDENCE_2' not in commands[0]['prompt']
        assert 'Current file state' in commands[0]['prompt']
        response = client.post('/v1/responses', json={'input': compact['output'] + [{'role': 'user', 'content': 'continue'}]})
        assert response.status_code == 200
        assert 'RECENT_EVIDENCE_1' in commands[-1]['prompt'] and 'RECENT_EVIDENCE_2' in commands[-1]['prompt']
        assert 'call_id="c2"' in commands[-1]['prompt']


def test_ccswitch_sync_preserves_other_presets_and_credentials(tmp_path):
    import sqlite3
    from configure_codex_context import update_ccswitch
    database = tmp_path / 'cc-switch.db'
    local = {'auth': {'api_key': 'LOCAL_TEST_ONLY'}, 'config': 'model = "gemini-4-carbon"\n[model_providers.local]\nbase_url = "http://127.0.0.1:8000/v1"\n'}
    other = {'auth': {'api_key': 'OTHER_TEST_ONLY'}, 'config': 'model = "gpt-6.1-sol"\n'}
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE providers (id TEXT, app_type TEXT, settings_config TEXT)')
        conn.executemany('INSERT INTO providers VALUES (?, ?, ?)', [('local', 'codex', json.dumps(local)), ('other', 'codex', json.dumps(other))])
    assert update_ccswitch(database, False, None) == ['CC Switch: local']
    backup = tmp_path / 'backup'
    update_ccswitch(database, True, backup)
    with sqlite3.connect(database) as conn:
        rows = {key: json.loads(raw) for key, raw in conn.execute('SELECT id,settings_config FROM providers')}
    assert rows['other'] == other and rows['local']['auth'] == local['auth']
    assert tomllib.loads(rows['local']['config'])['model_auto_compact_token_limit'] == CARBON_AUTO_COMPACT_TOKENS
    assert (backup / 'cc-switch.db').stat().st_mode & 0o777 == 0o600
    assert update_ccswitch(database, False, None) == []


def test_unicode_token_estimate_accounts_for_non_ascii_and_json_remains_reasonable():
    from schemas import estimate_tokens
    assert estimate_tokens('') == 0
    assert estimate_tokens('hello' * 100) == 125
    assert estimate_tokens('中文' * 100) == 300
    assert estimate_tokens('🙂' * 100) == 150
    sample = json.dumps({'a': [1, 2, 3], 'b': 'hello'}) * 100
    assert estimate_tokens(sample) == len(sample) // 4


def test_catalog_workflow_is_not_duplicated_by_bridge():
    from agent_policy import CODING_WORKFLOW
    messages = [ChatMessage(role='developer', content=CODING_WORKFLOW), ChatMessage(role='user', content='Read a file')]
    prepared = ConversationContextBuilder().build(messages, tools=[shell_tool()])
    assert prepared.prompt.count('[Coding workflow]') == 1

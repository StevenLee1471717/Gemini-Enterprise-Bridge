"""Regression checks for duplicate queries, streamed history, and model settings."""
import json

import pytest

from configure_codex_context import update_catalog, update_profile
from context_builder import ConversationContextBuilder
from schemas import ChatMessage
from tool_calling import (
    ToolStreamProcessor,
    ToolProtocolError,
    build_tool_delta_reminder,
    build_tool_system_instructions,
    parse_assistant_output,
)
from test_responses import browser, packets


def tools(name='functions.exec_command'):
    return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}]


def call(cmd='cat README.md', name='functions.exec_command'):
    return '```codex_tool_call\n' + json.dumps({'name': name, 'arguments': {'cmd': cmd}}) + '\n```\n'


def test_duplicate_queries_only_in_same_read_batch():
    assert len(parse_assistant_output(call() + call(), tools()).tool_calls) == 1
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call('printf changed >> data') * 2, tools())
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call('sed -i x file') * 2, tools())
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call() + call('touch file') + call(), tools())
    assert len(parse_assistant_output(call('nl -ba "some file.py" | sed -n \'1,100p\'') * 2, tools()).tool_calls) == 1
    assert len(parse_assistant_output(call('cat a') + call('cat b'), tools()).tool_calls) == 2
    malformed = '```codex_tool_call\n{"name":"functions.exec_command","arguments":[]}\n```\n'
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(malformed, tools())


@pytest.mark.parametrize('width', [1, 7, 1000])
def test_streamed_text_is_exactly_the_text_recorded_for_next_turn(width):
    raw = 'Checking files.\n\n' + call() + '\nI will continue after the result.'
    processor = ToolStreamProcessor(tools())
    emitted = []
    for i in range(0, len(raw), width):
        emitted.extend(processor.feed(raw[i:i+width]))
    tail, parsed = processor.finalize()
    assert ''.join(emitted + tail).strip() == (parsed.content or '').strip()
    tracker = ConversationContextBuilder()
    first = [ChatMessage(role='user', content='Check')]
    prepared = tracker.build(first, tools=tools())
    tracker.record_completed_turn('default', prepared.normalized_history, parsed.content, parsed.tool_calls)
    messages = first + [ChatMessage(role='assistant', content=''.join(emitted + tail), tool_calls=parsed.tool_calls),
                        ChatMessage(role='tool', content='OK', tool_call_id=parsed.tool_calls[0]['id'])]
    assert tracker.build(messages, tools=tools()).strategy_used == 'smart_incremental_delta'


def test_catalog_changes_only_gemini_and_is_repeatable():
    unrelated = {'slug': 'gpt-test', 'context_window': 99}
    source = json.dumps({'models': [unrelated, {'slug':'gemini-4-carbon'}, {'slug':'gemini-4-argon'}]})
    fixed = update_catalog(source)
    assert update_catalog(fixed) == fixed
    models = json.loads(fixed)['models']
    assert models[0] == unrelated
    for model in models[1:]:
        assert model['apply_patch_tool_type'] == 'freeform'
        assert model['truncation_policy'] == {'mode':'tokens', 'limit':10000}
        assert model['context_window'] == 1048576
        assert model['input_modalities'] == ['text', 'image']
    assert '1048576' in update_profile('model = "gemini-4-argon"\n')


def test_thinking_prefix_is_not_delivered_without_tools():
    processor = ToolStreamProcessor([])
    for ch in 'Thinking Process:\nprivate draft':
        assert not processor.feed(ch)
    with pytest.raises(ToolProtocolError):
        processor.finalize()


def test_explicit_suggestions_removed_but_normal_mentions_preserved():
    raw = 'Answer.\n[[suggested_questions]]\nTry this?\n[[/suggested_questions]]'
    processor = ToolStreamProcessor([])
    result = []
    for ch in raw:
        result.extend(processor.feed(ch))
    tail, parsed = processor.finalize()
    assert ''.join(result + tail).strip() == parsed.content == 'Answer.'
    normal = 'The field suggested_questions is documented here.'
    assert parse_assistant_output(normal).content == normal


def test_read_thread_limits_are_documented_and_normalized_before_delivery():
    spec = tools('mcp__codex_app.read_thread')
    spec[0]['function']['parameters']['properties'] = {'threadId': {'type':'string'}, 'turnLimit': {'type':'integer'}, 'maxOutputCharsPerItem': {'type':'integer'}}
    instructions = build_tool_system_instructions(spec)
    assert 'maximum' in instructions and '20000' in instructions
    raw = '```codex_tool_call\n' + json.dumps({'name':'mcp__codex_app.read_thread', 'arguments':{'threadId':'test','turnLimit':20,'maxOutputCharsPerItem':30000}}) + '\n```\n'
    args = json.loads(parse_assistant_output(raw, spec).tool_calls[0]['function']['arguments'])
    assert args == {'threadId':'test','turnLimit':10,'maxOutputCharsPerItem':20000}


@pytest.mark.parametrize('stream', [False, True])
def test_duplicates_do_not_escape_responses_boundary(stream):
    with browser([call() * 2]) as (client, _):
        result = client.post('/v1/responses', json={'input':'read', 'stream':stream,
            'tools':[{'type':'namespace','name':'functions','tools':[{'type':'function','name':'exec_command','parameters':{'type':'object'}}]}]})
        response = packets(result.text)[-1]['response'] if stream else result.json()
        assert len(response['output']) == 1
        assert response['output'][0]['type'] == 'function_call'


@pytest.mark.parametrize('width', [1, 11, 1000])
def test_suggestion_markers_in_fenced_examples_are_preserved(width):
    raw = 'Example:\n```text\n[[suggested_questions]]\nexample data\n```\nAnswer.'
    processor = ToolStreamProcessor([])
    emitted = []
    for i in range(0, len(raw), width):
        emitted.extend(processor.feed(raw[i:i+width]))
    tail, parsed = processor.finalize()
    assert ''.join(emitted + tail) == raw
    assert parsed.content == raw


def test_real_responses_round_trip_preserves_incremental_web_history():
    raw = 'Checking.\n\n' + call() + '\nWaiting for the result.'
    with browser([raw, 'Done.']) as (client, commands):
        declared = [{'type':'namespace', 'name':'functions', 'tools':[{'type':'function', 'name':'exec_command','parameters':{'type':'object'}}]}]
        response = packets(client.post('/v1/responses', json={'input':'Check the file','tools':declared,'stream':True}).text)[-1]['response']
        tool = next(x for x in response['output'] if x['type']=='function_call')
        result = client.post('/v1/responses', json={'input':[{'role':'user','content':'Check the file'}] + response['output'] +
            [{'type':'function_call_output','call_id':tool['call_id'],'output':'file checked'}], 'tools':declared})
        assert result.status_code == 200
        assert '[Conversation History]' not in commands[-1]['prompt']
        assert '[Tool Calling Protocol]' not in commands[-1]['prompt']


@pytest.mark.parametrize('width', [1, 5, 1024])
def test_fenced_suggested_questions_and_adjoined_tool_close_never_break(width):
    adjoined = (
        '正在读取配置。\n[[section:读取配置文件]]\n'
        '\u0060\u0060\u0060codex_tool_call\n'
        + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': 'rg -n "foo" src | sed -n "1,20p"'}})
        + '\n\u0060\u0060\u0060\u0060\u0060\u0060suggested_questions\n{"questions":["Next?"],"type":"followup"}\n\u0060\u0060\u0060'
    )
    processor = ToolStreamProcessor(tools())
    emitted = []
    for i in range(0, len(adjoined), width):
        emitted.extend(processor.feed(adjoined[i : i + width]))
    tail, parsed = processor.finalize()
    assert ''.join(emitted + tail).strip() == '正在读取配置。'
    assert len(parsed.tool_calls) == 1
    assert 'suggested_questions' not in (parsed.content or '')

    prose_with_fenced_sq = (
        '检查完毕。\n\n\u0060\u0060\u0060suggested_questions\n{"questions":["Q1?","Q2?"],"type":"followup"}\n\u0060\u0060\u0060\n'
    )
    p2 = ToolStreamProcessor([])
    out2 = []
    for i in range(0, len(prose_with_fenced_sq), width):
        out2.extend(p2.feed(prose_with_fenced_sq[i : i + width]))
    t2, parsed2 = p2.finalize()
    assert ''.join(out2 + t2).strip() == parsed2.content == '检查完毕。'


def test_parallel_safe_read_pipelines_and_multi_object_fence():
    multi_obj = (
        '\u0060\u0060\u0060codex_tool_call\n'
        + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': 'git status --short && rg --files work'}})
        + '\n'
        + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': "nl -ba README.md | sed -n '1,40p' 2>/dev/null"}})
        + '\n\u0060\u0060\u0060\n'
    )
    parsed = parse_assistant_output(multi_obj, tools())
    assert len(parsed.tool_calls) == 2
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call('git status') + call('git checkout main'), tools())
    instructions = build_tool_system_instructions(tools())
    assert 'RFC 8259' in instructions and 'Markdown backslash escaping' in instructions
    assert 'Batch independent read-only inspections in the same turn' in instructions
    assert 'Disjoint file edits' in instructions
    assert 'strictly ONE per turn' in instructions
    reminder = build_tool_delta_reminder(tools(), has_tool_results=True)
    assert 'batch independent read-only lookups or disjoint file patches' in reminder
    assert 'emit one block for non-read commands/tests' in reminder
    serial_instructions = build_tool_system_instructions(tools(), parallel_tool_calls=False)
    assert 'Emit at most ONE codex_tool_call code block in this turn' in serial_instructions
    serial_reminder = build_tool_delta_reminder(tools(), has_tool_results=True, parallel_tool_calls=False)
    assert 'emit one fenced codex_tool_call JSON block' in serial_reminder


def test_real_world_read_queries_coordinator_tools_and_disjoint_patches():
    multi_tools = [
        {'type': 'function', 'function': {'name': 'functions.exec_command', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'functions.apply_patch', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'functions.view_image', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'functions.update_plan', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'functions.shell_command', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'mcp__codegraph.codegraph_search', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'mcp__github.search_code', 'parameters': {'type': 'object'}}},
    ]

    def fence_call(name, args):
        return '```codex_tool_call\n' + json.dumps({'name': name, 'arguments': args}, ensure_ascii=False) + '\n```\n'

    # 1. Real-world read-only shell commands (cd, single-quoted backticks, sed regex/step, awk, codegraph, node -v/--check, git branch/remote)
    rich_reads = (
        fence_call('functions.exec_command', {'cmd': "cd work && rg -n '`template`' src/ 2>/dev/null || true"})
        + fence_call('functions.exec_command', {'cmd': "sed -n '/function init/,+40p' work/src/main.js"})
        + fence_call('functions.exec_command', {'cmd': "awk 'NR>=100 && NR<=200' work/src/main.js"})
        + fence_call('functions.exec_command', {'cmd': 'node -v && npm -v && node --check work/src/main.js && codegraph explore "main.js" && git branch --show-current && git remote -v >/dev/null'})
        + fence_call('functions.shell_command', {'command': 'git worktree list --porcelain && git stash list'})
        + fence_call('mcp__codegraph.codegraph_search', {'query': 'ToolStreamProcessor'})
        + fence_call('mcp__github.search_code', {'query': 'codex_tool_call repo:o/r'})
    )
    assert len(parse_assistant_output(rich_reads, multi_tools).tool_calls) == 7

    # Unsafe variants of those commands must still be rejected in multi-call batches
    for unsafe_cmd in [
        "sed -n '1,10w out.txt' a.txt",
        'awk \'{system("id")}\' a.txt',
        'sort -o out.txt a.txt',
        'echo "`id`"',
        'git branch new-feature',
        'git worktree add ../wt main',
        'git stash push -m wip',
        'node -e "console.log(1)"',
    ]:
        with pytest.raises(ToolProtocolError):
            parse_assistant_output(call('cat a.txt') + call(unsafe_cmd), multi_tools)
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(
            call('cat a.txt') + fence_call('functions.shell_command', {'command': 'pytest -q'}),
            multi_tools,
        )

    # 2. Coordinator / UI tools alongside a single workspace command or patch
    img_plus_ocr = (
        fence_call('functions.view_image', {'path': '/tmp/shot.png'})
        + fence_call('functions.exec_command', {'cmd': "swift -e 'print(1)'"})
    )
    assert len(parse_assistant_output(img_plus_ocr, multi_tools).tool_calls) == 2

    patch_a = '*** Begin Patch\n*** Add File: work/lan_server.mjs\n+export const port = 8787;\n*** End Patch'
    patch_b = '*** Begin Patch\n*** Add File: work/src/lan_client.js\n+export const host = "127.0.0.1";\n*** End Patch'
    plan_plus_patch = (
        fence_call('functions.update_plan', {'plan': [{'step': 'Add LAN', 'status': 'in_progress'}]})
        + fence_call('functions.apply_patch', {'input': patch_a})
    )
    assert len(parse_assistant_output(plan_plus_patch, multi_tools).tool_calls) == 2

    # Duplicate non-read coordinator calls still fail
    dup_plan = fence_call('functions.update_plan', {'plan': []}) * 2
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(dup_plan, multi_tools)

    # 3. Disjoint-file apply_patch batch succeeds and preserves smart_incremental_delta
    disjoint_patches = (
        fence_call('functions.apply_patch', {'input': patch_a})
        + fence_call('functions.apply_patch', {'input': patch_b})
    )
    parsed_disjoint = parse_assistant_output(disjoint_patches, multi_tools)
    assert len(parsed_disjoint.tool_calls) == 2

    # Same-file or aliasing relative/absolute apply_patch batches still fail
    patch_a_rel = '*** Begin Patch\n*** Update File: ./work/lan_server.mjs\n@@\n-export const port = 8787;\n+export const port = 8788;\n*** End Patch'
    patch_a_abs = '*** Begin Patch\n*** Update File: /Users/test/repo/work/lan_server.mjs\n@@\n-export const port = 8787;\n+export const port = 8788;\n*** End Patch'
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(
            fence_call('functions.apply_patch', {'input': patch_a})
            + fence_call('functions.apply_patch', {'input': patch_a_rel}),
            multi_tools,
        )
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(
            fence_call('functions.apply_patch', {'input': patch_a})
            + fence_call('functions.apply_patch', {'input': patch_a_abs}),
            multi_tools,
        )
    # Patch + shell command in same turn still fails
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(
            fence_call('functions.apply_patch', {'input': patch_a})
            + fence_call('functions.exec_command', {'cmd': 'ls -la work/lan_server.mjs'}),
            multi_tools,
        )

    # Verify full Responses API round-trip with 2 disjoint custom_tool_call patches keeps incremental session
    resp_tools = [
        {'type': 'custom', 'name': 'apply_patch'},
        {'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object'}},
    ]
    raw_two_patches = (
        fence_call('apply_patch', {'input': patch_a})
        + fence_call('apply_patch', {'input': patch_b})
    )
    with browser([raw_two_patches, 'LAN modules created.']) as (client, commands):
        r1 = client.post('/v1/responses', json={'input': 'Create LAN modules', 'tools': resp_tools, 'stream': True})
        resp1 = packets(r1.text)[-1]['response']
        assert resp1['status'] == 'completed'
        calls_out = [o for o in resp1['output'] if o['type'] == 'custom_tool_call']
        assert len(calls_out) == 2
        r2 = client.post(
            '/v1/responses',
            json={
                'previous_response_id': resp1['id'],
                'tools': resp_tools,
                'input': [
                    {'type': 'custom_tool_call_output', 'call_id': calls_out[0]['call_id'], 'output': 'Success.'},
                    {'type': 'custom_tool_call_output', 'call_id': calls_out[1]['call_id'], 'output': 'Success.'},
                ],
            },
        )
        assert r2.status_code == 200
        assert not commands[-1]['reset_chat']
        assert '[Conversation History]' not in commands[-1]['prompt']


def test_system_diagnostic_reads_conflict_reason_and_turn1_retry_notice():
    from agent_policy import CODING_WORKFLOW

    diag_reads = (
        call('ls -la "$HOME/Library/LaunchAgents" && cat $HOME/.codex/config.toml ${CODEX_HOME}/settings.json')
        + call("launchctl list | grep -E 'gemini|router' && launchctl print gui/501/local.gemini-enterprise-bridge")
        + call('plutil -p ~/Library/LaunchAgents/local.gemini-enterprise-bridge.plist && plutil -lint -s foo.plist')
        + call('defaults read com.apple.finder && sysctl -n hw.ncpu && scutil --dns && scutil --nc list && pmset -g assertions && vm_stat && dig example.com')
    )
    assert len(parse_assistant_output(diag_reads, tools()).tool_calls) == 4

    for unsafe_cmd in [
        'launchctl kickstart -k gui/501/local.gemini-enterprise-bridge',
        "plutil -replace ProgramArguments -json '[]' foo.plist",
        'defaults write com.apple.finder AppleShowAllFiles -bool true',
        'sysctl -w net.inet.ip.forwarding=1',
        'scutil --nc start VPN',
        'pmset -a displaysleep 0',
        'ls -la "$(whoami)"',
        'ls -la "${HOME:-/tmp}"',
        '"$HOME/bin/ls" -la',
    ]:
        with pytest.raises(ToolProtocolError):
            parse_assistant_output(call('cat README.md') + call(unsafe_cmd), tools())

    with pytest.raises(ToolProtocolError) as exc_info:
        parse_assistant_output(
            call("sed -n '1,20p' README.md") + call("python3 -c 'print(1)'"),
            tools(),
        )
    err_text = str(exc_info.value)
    assert '文件修改或命令执行必须等待真实结果后再检查' in err_text
    assert "exec_command('python3 -c" in err_text

    instructions = build_tool_system_instructions(tools())
    reminder = build_tool_delta_reminder(tools(), has_tool_results=True)
    assert 'never pair python/node/curl/scripts alongside' in instructions
    assert 'never mix python/node scripts, curl' in reminder
    assert 'Never mix python/node one-liners, curl' in CODING_WORKFLOW

    resp_tools = [{'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object'}}]
    mixed_turn1 = (
        '```codex_tool_call\n'
        + json.dumps({'name': 'exec_command', 'arguments': {'cmd': "sed -n '1,20p' README.md"}})
        + '\n```\n```codex_tool_call\n'
        + json.dumps({'name': 'exec_command', 'arguments': {'cmd': "python3 -c 'print(1)'"}})
        + '\n```\n'
    )
    single_ok = (
        '```codex_tool_call\n'
        + json.dumps({'name': 'exec_command', 'arguments': {'cmd': "python3 -c 'print(1)'"}})
        + '\n```\n'
    )
    with browser([mixed_turn1, 'unrelated ok', mixed_turn1, single_ok]) as (client, commands):
        fail1 = client.post('/v1/responses', json={'input': 'Restart gateway', 'tools': resp_tools, 'stream': True})
        assert packets(fail1.text)[-1]['type'] == 'response.failed'
        # Unrelated request must NOT inherit a stale Retry Notice from a different Turn-1 prompt
        other = client.post('/v1/responses', json={'input': 'Different task entirely', 'tools': resp_tools})
        assert other.status_code == 200
        assert 'Retry Notice' not in commands[-1]['prompt']

        # Multi-message Turn-1 request (e.g. @thread reference + user request) gets exact Retry Notice on retry
        multi_input = [
            {'role': 'user', 'content': '## Referenced chats with Codex'},
            {'role': 'user', 'content': 'Restart gateway and check status'},
        ]
        fail2 = client.post('/v1/responses', json={'input': multi_input, 'tools': resp_tools, 'stream': True})
        assert packets(fail2.text)[-1]['type'] == 'response.failed'
        retry2 = client.post('/v1/responses', json={'input': multi_input, 'tools': resp_tools})
        assert retry2.status_code == 200
        assert commands[-1]['reset_chat'] is True
        assert '[Retry Notice — Previous Reply Had a Format Error; Please Rewrite]' in commands[-1]['prompt']
        assert "exec_command('python3 -c" in commands[-1]['prompt']


def test_qpd_batching_allows_non_overlapping_same_file_edits_and_discovery_reads():
    edit_tools = [
        {'type': 'function', 'function': {'name': 'edit', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'create', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'view', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'tool_search_tool', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'session_store_sql', 'parameters': {'type': 'object'}}},
    ]

    def fc(name, args):
        return '```codex_tool_call\n' + json.dumps({'name': name, 'arguments': args}, ensure_ascii=False) + '\n```\n'

    # 1. Multiple non-overlapping `edit` blocks on the SAME file + another file in one turn succeed
    batched_edits = (
        fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'PORT = 8000', 'new_str': 'PORT = 9000'})
        + fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'DEBUG = False', 'new_str': 'DEBUG = True'})
        + fc('create', {'path': 'I:\\repo\\helper.py', 'file_text': 'def ok():\n    return True\n'})
    )
    parsed = parse_assistant_output(batched_edits, edit_tools)
    assert len(parsed.tool_calls) == 3

    # 2. Overlapping `old_str` or `new_str` colliding with subsequent `old_str` on the same file must reject
    overlapping_old = (
        fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'PORT = 8000', 'new_str': 'PORT = 9000'})
        + fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'PORT = 8000\nDEBUG = False', 'new_str': 'PORT = 9001\nDEBUG = True'})
    )
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(overlapping_old, edit_tools)

    chained_collision = (
        fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'ALPHA = 1', 'new_str': 'BETA = 2'})
        + fc('edit', {'path': 'I:\\repo\\app.py', 'old_str': 'BETA = 2', 'new_str': 'GAMMA = 3'})
    )
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(chained_collision, edit_tools)

    # 3. `create` + `edit` on the same file in one turn must still reject
    create_plus_edit_same = (
        fc('create', {'path': 'I:\\repo\\new.py', 'file_text': 'X = 1\n'})
        + fc('edit', {'path': 'I:\\repo\\new.py', 'old_str': 'X = 1', 'new_str': 'X = 2'})
    )
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(create_plus_edit_same, edit_tools)

    # 4. Read-only discovery tools (`tool_search_tool`, `session_store_sql`) batch cleanly with `view`
    batched_reads = (
        fc('tool_search_tool', {'query': 'github issue'})
        + fc('session_store_sql', {'description': 'recent sessions', 'query': 'SELECT id FROM sessions LIMIT 5'})
        + fc('view', {'path': 'I:\\repo\\app.py', 'view_range': [1, 600]})
        + fc('view', {'path': 'I:\\repo\\app.py', 'view_range': [601, 1200]})
    )
    assert len(parse_assistant_output(batched_reads, edit_tools).tool_calls) == 4

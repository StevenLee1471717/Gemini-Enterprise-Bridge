"""Regression checks for the truncated CodeIsland tool reply and exposed planning text."""
import json

import pytest

from tool_calling import ToolProtocolError, ToolStreamProcessor, parse_assistant_output
from test_responses import browser, packets


def tools(*names):
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]


def call(name="exec_command", cmd="printf verified"):
    return '```codex_tool_call\n' + json.dumps({"name": name, "arguments": {"cmd": cmd}}) + '\n```\n'


@pytest.mark.parametrize('width', [1, 7, 51, 1024])
def test_namespace_added_to_existing_chat_preserves_entire_tool_call(width):
    raw = call(name="functions.exec_command", cmd="read full reply\n" + "x" * 620)
    processor = ToolStreamProcessor(tools('functions.exec_command'))
    output = []
    for start in range(0, len(raw), width):
        output.extend(processor.feed(raw[start:start+width]))
    tail, parsed = processor.finalize()
    assert output + tail == []
    assert parsed.tool_calls[0]['function']['name'] == 'functions.exec_command'
    assert json.loads(parsed.tool_calls[0]['function']['arguments'])['cmd'] == 'read full reply\n' + 'x' * 620


def test_ambiguous_short_name_and_wrong_namespace_are_rejected():
    for name in ['exec_command', 'other.exec_command']:
        with pytest.raises(ToolProtocolError):
            parse_assistant_output(call(name), tools('a.exec_command', 'b.exec_command'))


@pytest.mark.parametrize('raw', [
    '```codex_tool_call\n{"name": "exec_command", "arguments": {',
    call('missing'),
    call() + '```codex_tool_call\n{"name":"exec_command","arguments":{',
])
def test_invalid_reply_never_becomes_a_completed_text_answer(raw):
    processor = ToolStreamProcessor(tools('exec_command'))
    assert processor.feed(raw[:51]) == []
    assert processor.feed(raw[51:]) == []
    with pytest.raises(ToolProtocolError):
        processor.finalize()


@pytest.mark.parametrize('prefix', [
    'Thinking Process:\n', '**Thinking Process:**\n', '**Thinking Process**:\n',
    '思考过程：\n', '思考过程:\n', '**思考过程：**\n', '**思考过程**:\n',
    '[[section:思考]]\n# 思考过程:\n', '思维过程：\n', '内部思考：\n',
    '<think>', '[[section:Planning]]Thinking Process:\n',
])
def test_internal_planning_is_held_back_even_when_split_one_character_at_a_time(prefix):
    processor = ToolStreamProcessor(tools('exec_command'))
    for ch in prefix + 'internal draft only':
        assert processor.feed(ch) == []
    with pytest.raises(ToolProtocolError):
        processor.finalize()


def test_incomplete_closing_fence_is_rejected():
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call().removesuffix('\n```\n'), tools('exec_command'))


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('bad', ['Thinking Process:\ninternal draft only', '思考过程：\ninternal draft only', '```codex_tool_call\n{"name":"exec_command","arguments":{'])
def test_bad_reply_fails_once_without_resubmission_or_exposure(stream, bad):
    request_tools = [{'type':'function','name':'exec_command','parameters':{'type':'object'}}]
    with browser([bad]) as (client,commands):
        result=client.post('/v1/responses',json={'input':'check','tools':request_tools,'stream':stream})
        if stream:
            events=packets(result.text)
            assert events[-1]['type']=='response.failed'
            assert not any(e['type']=='response.completed' for e in events)
        else:
            assert result.status_code==502
        assert 'internal draft only' not in result.text
        assert len(commands)==1


def test_repeated_invalid_output_fails_without_fake_completion():
    with browser(['```codex_tool_call\n{']) as (client, commands):
        events = packets(client.post('/v1/responses', json={'input':'check', 'stream':True,
            'tools':[{'type':'function', 'name':'exec_command', 'parameters':{'type':'object'}}]}).text)
        assert events[-1]['type'] == 'response.failed'
        assert not any(e['type']=='response.completed' for e in events)
        assert len(commands) == 1


def test_missing_browser_reply_chunk_is_explicit_failure():
    with browser([{'type':'request_complete', 'chunked':True, 'total_chunks':2}]) as (client, _):
        events = packets(client.post('/v1/responses', json={'input':'test', 'stream':True}).text)
        assert events[-1]['type'] == 'response.failed'
        assert '分段缺失' in events[-1]['response']['error']['message']


def test_normal_chinese_answers_code_and_tool_args_mentioning_thinking_are_preserved():
    prose = '思考过程在工程验收中应当简洁透明，结论如下：全部通过。'
    code = '示例代码：\n```python\nlabel = "思考过程：不应误删代码块"\n```'
    for sample in (prose, code):
        processor = ToolStreamProcessor(tools('exec_command'))
        emitted = []
        for ch in sample:
            emitted.extend(processor.feed(ch))
        tail, parsed = processor.finalize()
        assert ''.join(emitted + tail) == sample
        assert parsed.content == sample

    tool_with_arg = call(cmd='printf "思考过程：保留工具参数"')
    parsed_tool = parse_assistant_output('正在执行检查。\n\n' + tool_with_arg, tools('exec_command'))
    assert parsed_tool.content == '正在执行检查。'
    assert json.loads(parsed_tool.tool_calls[0]['function']['arguments'])['cmd'] == 'printf "思考过程：保留工具参数"'


def test_configure_codex_context_enables_text_and_image_modalities():
    from configure_codex_context import update_catalog
    raw = json.dumps({
        'models': [
            {'slug': 'gemini-4-carbon', 'input_modalities': ['text']},
            {'slug': 'gpt-5', 'input_modalities': ['text']},
        ]
    })
    saved = json.loads(update_catalog(raw))
    assert saved['models'][0]['input_modalities'] == ['text', 'image']
    assert saved['models'][1]['input_modalities'] == ['text']


@pytest.mark.parametrize('width', [1, 2, 3, 7, 51, 1024])
def test_local_file_and_app_paths_become_clickable_markdown_links_across_chunks(width, tmp_path):
    from web_text import clean_web_text

    folder_only = tmp_path
    raw = (
        '**你可以直接双击体验的位置**\n'
        '- 双击启动器：`/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/余烬圣所 · 第三人称版.app`\n'
        '- 或直接打开网页版：`/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html`\n'
        '- 代码位置：`/Users/alice/Documents/Projects/gemini bridge/work/Gemini-Enterprise-Bridge/schemas.py:229`\n'
        f'- 目录保留：`{folder_only}` 与接口 `/api/status` 及命令 `python3 /Users/alice/run.py`\n'
        '- 已有链接保持：[说明](</Users/alice/Documents/Projects/gemini bridge/README.md>) 与 [`标签`](/Users/alice/a.py)\n'
        '```sh\n'
        'open "/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html"\n'
        'p=`/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html`\n'
        '```\n'
        + call(cmd='ls "/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html"')
    )
    expected_prose = (
        '**你可以直接双击体验的位置**\n'
        '- 双击启动器：[余烬圣所 · 第三人称版.app](</Users/alice/Documents/Projects/roguelike/tps-edition/outputs/余烬圣所 · 第三人称版.app>)\n'
        '- 或直接打开网页版：[sanctum-of-ash-tps.html](/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html)\n'
        '- 代码位置：[schemas.py:229](</Users/alice/Documents/Projects/gemini bridge/work/Gemini-Enterprise-Bridge/schemas.py:229>)\n'
        f'- 目录保留：`{folder_only}` 与接口 `/api/status` 及命令 `python3 /Users/alice/run.py`\n'
        '- 已有链接保持：[说明](</Users/alice/Documents/Projects/gemini bridge/README.md>) 与 [`标签`](/Users/alice/a.py)\n'
        '```sh\n'
        'open "/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html"\n'
        'p=`/Users/alice/Documents/Projects/roguelike/tps-edition/outputs/sanctum-of-ash-tps.html`\n'
        '```'
    )

    processor = ToolStreamProcessor(tools('exec_command'))
    emitted = []
    for start in range(0, len(raw), width):
        emitted.extend(processor.feed(raw[start : start + width]))
    tail, parsed = processor.finalize()
    assert ''.join(emitted + tail).strip() == expected_prose
    assert parsed.content == expected_prose
    assert parsed.has_tool_calls
    assert clean_web_text(raw.split('```codex_tool_call')[0]).strip() == expected_prose


def test_legacy_inline_code_file_reference_rule_is_upgraded_in_catalog_and_context():
    from configure_codex_context import update_catalog
    from context_builder import ConversationContextBuilder
    from schemas import ChatMessage

    legacy = (
        'Header\n'
        '- File References: When referencing files in your response, make sure to include the relevant start line and always follow the below rules:\n'
        '  * Use inline code to make file paths clickable.\n'
        '  * Do not use URIs like file://, vscode://, or https://.\n'
        'Footer'
    )
    catalog = json.dumps({'models': [{'slug': 'gemini-4-carbon', 'base_instructions': legacy}]})
    updated = update_catalog(catalog)
    assert update_catalog(updated) == updated
    carbon_inst = json.loads(updated)['models'][0]['base_instructions']
    assert 'Use inline code to make file paths clickable' not in carbon_inst
    assert 'prefer a clickable markdown link' in carbon_inst

    builder = ConversationContextBuilder()
    prepared = builder.build(
        [ChatMessage(role='developer', content=legacy), ChatMessage(role='user', content='hi')],
        tools=tools('exec_command'),
    )
    assert 'Use inline code to make file paths clickable' not in prepared.prompt
    assert 'prefer a clickable markdown link' in prepared.prompt
    assert prepared.system_fingerprint == legacy


def test_curated_desktop_baseline_replaces_legacy_cli_and_raw_gpt6_while_preserving_bridge_core():
    from agent_policy import GEMINI_DESKTOP_IDENTITY
    from configure_codex_context import update_catalog
    from context_builder import ConversationContextBuilder
    from schemas import ChatMessage

    legacy_cli = (
        "You are Codex. You are running as a coding agent in the Codex CLI on a user's computer.\n"
        "While you are working, you might notice unexpected changes that you didn't make. If this happens, STOP IMMEDIATELY and ask the user how they would like to proceed.\n"
        "You are producing plain text that will later be styled by the CLI."
    )
    raw_gpt6 = (
        "You are Codex, an agent based on GPT-6.\n"
        "You share updates in the `commentary` channel.\n"
        "Use `functions.request_user_input_async` and `skills.list`."
    )
    catalog = json.dumps({
        'models': [
            {
                'slug': 'gemini-4-carbon',
                'base_instructions': legacy_cli,
                'model_messages': {
                    'instructions_template': legacy_cli,
                    'persistent_instructions': 'Use functions.send_user_message_async',
                    'guardian_v2': 'internal',
                },
            },
            {
                'slug': 'gemini-4-argon',
                'model_messages': {'instructions_template': raw_gpt6},
            },
        ]
    })
    updated = update_catalog(catalog)
    assert update_catalog(updated) == updated
    models = {m['slug']: m for m in json.loads(updated)['models']}
    for slug in ('gemini-4-carbon', 'gemini-4-argon'):
        bi = models[slug]['base_instructions']
        tpl = models[slug]['model_messages']['instructions_template']
        assert bi == tpl
        assert bi.startswith(GEMINI_DESKTOP_IDENTITY)
        assert '[Coding workflow]' in bi
        assert 'styled by the CLI' not in bi
        assert 'STOP IMMEDIATELY' not in bi
        assert '`commentary` channel' not in bi
        assert 'request_user_input_async' not in bi
        assert 'persistent_instructions' not in models[slug]['model_messages']
        assert 'guardian_v2' not in models[slug]['model_messages']
        assert models[slug]['input_modalities'] == ['text', 'image']
        assert models[slug]['apply_patch_tool_type'] == 'freeform'

    desktop_ctx = '# Codex desktop context\n## User Profile\nCustom user memory and local SKILL.md list.'
    builder = ConversationContextBuilder()
    prepared = builder.build(
        [
            ChatMessage(role='system', content=legacy_cli),
            ChatMessage(role='developer', content=desktop_ctx),
            ChatMessage(role='user', content='inspect repo'),
        ],
        tools=tools('exec_command'),
    )
    assert GEMINI_DESKTOP_IDENTITY in prepared.prompt
    assert desktop_ctx in prepared.prompt
    assert 'STOP IMMEDIATELY' not in prepared.prompt
    assert 'styled by the CLI' not in prepared.prompt
    assert 'codex_tool_call' in prepared.prompt
    assert 'Uploaded screenshots are visual evidence' in prepared.prompt
    assert prepared.prompt.count('[Coding workflow]') == 1
    assert prepared.system_fingerprint == f'{legacy_cli}\n\n{desktop_ctx}'

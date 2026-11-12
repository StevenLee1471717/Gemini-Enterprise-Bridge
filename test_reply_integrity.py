"""Regression checks for the truncated CodeIsland tool reply and exposed planning text."""
import json

import pytest

from tool_calling import ToolProtocolError, ToolStreamProcessor, parse_assistant_output
from test_responses import browser, packets


def tools(*names):
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]


def call(name="exec_command", cmd="printf verified"):
    return '[TOOL_CALL]' + json.dumps({"name": name, "arguments": {"cmd": cmd}}) + '[/TOOL_CALL]'


@pytest.mark.parametrize('width', [1, 7, 51, 1024])
def test_namespace_added_to_existing_chat_preserves_entire_tool_call(width):
    raw = call(cmd="read full reply\n" + "x" * 620)
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
    '[TOOL_CALL]{"name": "exec_command", "arguments": {',
    call('missing'),
    call() + '[TOOL_CALL]{"name":"exec_command","arguments":{',
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


def test_incomplete_closing_tag_can_still_recover_complete_json():
    parsed = parse_assistant_output(call().removesuffix('[/TOOL_CALL]'), tools('exec_command'))
    assert len(parsed.tool_calls) == 1


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('bad', ['Thinking Process:\ninternal draft only', '思考过程：\ninternal draft only', '[TOOL_CALL]{"name":"exec_command","arguments":{'])
def test_bad_reply_is_repaired_once_without_exposing_it(stream, bad):
    request_tools = [{'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object'}}]
    with browser([bad, call()]) as (client, commands):
        result = client.post('/v1/responses', json={'input':'check', 'tools':request_tools, 'stream':stream})
        response = packets(result.text)[-1]['response'] if stream else result.json()
        assert response['status'] == 'completed'
        assert response['output'][0]['type'] == 'function_call'
        assert json.loads(response['output'][0]['arguments']) == {'cmd':'printf verified'}
        assert 'internal draft only' not in result.text
        assert len(commands) == 2 and not commands[-1]['reset_chat']
        assert 'No tool from that reply was executed' in commands[-1]['prompt']


def test_repeated_invalid_output_fails_without_fake_completion():
    with browser(['[TOOL_CALL]{', '[TOOL_CALL]{']) as (client, commands):
        events = packets(client.post('/v1/responses', json={'input':'check', 'stream':True,
            'tools':[{'type':'function', 'name':'exec_command', 'parameters':{'type':'object'}}]}).text)
        assert events[-1]['type'] == 'response.failed'
        assert not any(e['type']=='response.completed' for e in events)
        assert len(commands) == 2


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

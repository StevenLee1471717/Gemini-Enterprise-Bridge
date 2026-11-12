"""Regression checks for duplicate queries, streamed history, and model settings."""
import json

import pytest

from configure_codex_context import update_catalog, update_profile
from context_builder import ConversationContextBuilder
from schemas import ChatMessage
from tool_calling import ToolStreamProcessor, ToolProtocolError, build_tool_system_instructions, parse_assistant_output
from test_responses import browser, packets


def tools(name='functions.exec_command'):
    return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}]


def call(cmd='cat README.md', name='functions.exec_command'):
    return '[TOOL_CALL]' + json.dumps({'name': name, 'arguments': {'cmd': cmd}}) + '[/TOOL_CALL]'


def test_duplicate_queries_only_in_same_read_batch():
    assert len(parse_assistant_output(call() + call(), tools()).tool_calls) == 1
    assert len(parse_assistant_output(call('printf changed >> data') * 2, tools()).tool_calls) == 2
    assert len(parse_assistant_output(call('sed -i x file') * 2, tools()).tool_calls) == 2
    assert len(parse_assistant_output(call() + call('touch file') + call(), tools()).tool_calls) == 3
    assert len(parse_assistant_output(call('nl -ba "some file.py" | sed -n \'1,100p\'') * 2, tools()).tool_calls) == 1
    assert len(parse_assistant_output(call('cat a') + call('cat b'), tools()).tool_calls) == 2
    malformed = '[TOOL_CALL]{"name":"functions.exec_command","arguments":[]}[/TOOL_CALL]'
    assert len(parse_assistant_output(malformed, tools()).tool_calls) == 1


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
    raw = '[TOOL_CALL]' + json.dumps({'name':'mcp__codex_app.read_thread', 'arguments':{'threadId':'test','turnLimit':20,'maxOutputCharsPerItem':30000}}) + '[/TOOL_CALL]'
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

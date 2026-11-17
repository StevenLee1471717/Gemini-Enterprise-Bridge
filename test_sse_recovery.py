"""Client event semantics and bounded recovery for interrupted large histories."""
import asyncio
import json
import tomllib

import pytest
from fastapi.responses import StreamingResponse

import responses_api
from configure_codex_context import update_stream_timeouts
from context_builder import ConversationContextBuilder
from schemas import ChatMessage
from streaming_utils import with_keepalive
from test_responses import browser, packets


@pytest.mark.parametrize('stream', [False, True])
def test_tool_preamble_is_commentary_and_answer_is_final(stream):
    tool = '```codex_tool_call\n{"name":"exec_command","arguments":{"cmd":"printf ok"}}\n```\n'
    with browser(['Checking.\n\n' + tool, 'Verified.']) as (client, _):
        tools = [{'type':'function','name':'exec_command','parameters':{'type':'object'}}]
        first = client.post('/v1/responses', json={'input':'check', 'tools':tools, 'stream':stream})
        response = packets(first.text)[-1]['response'] if stream else first.json()
        assert response['output'][0]['phase'] == 'commentary'
        call = response['output'][1]
        second = client.post('/v1/responses', json={'previous_response_id':response['id'], 'tools':tools,
            'input':[{'type':'function_call_output','call_id':call['call_id'],'output':'ok'}], 'stream':stream})
        response = packets(second.text)[-1]['response'] if stream else second.json()
        assert response['output'][0]['phase'] == 'final_answer'
        if stream:
            done = [e['item'] for e in packets(second.text) if e['type'] == 'response.output_item.done']
            assert done[0]['phase'] == 'final_answer'


def test_keepalive_is_valid_small_json_with_ordered_sequence(monkeypatch):
    monkeypatch.setattr(responses_api, 'with_keepalive', lambda s, **kw: with_keepalive(s, interval=.005, **kw))
    async def source():
        await asyncio.sleep(.04)
        yield 'data: ' + json.dumps({'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}]}) + '\n\n'
        yield 'data: [DONE]\n\n'
    async def run():
        response = await responses_api.translate_response(StreamingResponse(source()),
            {'tools':[{'description':'x'*200000}]}, 'gemini-4-carbon', {}, [], responses_api.ResponseStore())
        return ''.join([frame async for frame in response.body_iterator])
    events = packets(asyncio.run(run()))
    assert events[-1]['type'] == 'response.completed'
    progress = [e for e in events[2:] if e['type'] == 'response.in_progress']
    assert len(progress) >= 2
    assert all(len(json.dumps(e)) < 600 and e['response']['output'] == [] for e in progress)
    assert [e['sequence_number'] for e in events] == list(range(len(events)))


def completed_large_history():
    builder = ConversationContextBuilder()
    history = [ChatMessage(role='user', content='KEEP_FULL_SOURCE\n' + 'x'*100000),
               ChatMessage(role='assistant', content='recorded'), ChatMessage(role='user', content='continue')]
    prepared = builder.build(history, model='gemini-4-carbon')
    builder.record_completed_turn(session_key='default', sent_history=prepared.normalized_history,
        assistant_reply='second answer', tools_fingerprint=prepared.tools_fingerprint,
        system_fingerprint=prepared.system_fingerprint, active_model=prepared.target_model,
        logical_prompt_tokens=100000)
    history += [ChatMessage(role='assistant', content='second answer'), ChatMessage(role='user', content='latest input')]
    return builder, history


def test_interrupted_turn_invalidates_page_without_retry():
    builder, history = completed_large_history()
    builder.invalidate_session('default')
    retry = builder.build(history, model='gemini-4-carbon')
    assert retry.reset_chat
    assert 'Retry Notice' not in retry.prompt and 'latest input' in retry.prompt
    assert 'KEEP_FULL_SOURCE' in retry.prompt
    builder.invalidate_session('default')
    rebuilt = builder.build(history, model='gemini-4-carbon')
    assert rebuilt.reset_chat and 'KEEP_FULL_SOURCE' in rebuilt.prompt


def test_invalidated_page_rebuild_does_not_reuse_a_different_chat():
    builder, history = completed_large_history()
    builder.invalidate_session('default')
    retry = builder.build(history, model='gemini-4-carbon')
    builder.record_completed_turn(session_key='default', sent_history=retry.normalized_history,
        assistant_reply='recovered', system_fingerprint=retry.system_fingerprint)
    next_turn = history + [ChatMessage(role='assistant', content='recovered'), ChatMessage(role='user', content='again')]
    assert 'Retry Notice' not in builder.build(next_turn, model='gemini-4-carbon').prompt
    builder.invalidate_session('default')
    other = [ChatMessage(role='user', content='different chat'), ChatMessage(role='assistant', content='hello'),
             ChatMessage(role='user', content='next')]
    assert builder.build(other, model='gemini-4-carbon').reset_chat


@pytest.mark.parametrize('stream', [False, True])
def test_http_large_history_recovery_keeps_completed_turns(stream):
    history = [{'role':'user','content':'KEEP_FULL_SOURCE\n' + 'x'*120000},
               {'role':'assistant','content':'recorded'}, {'role':'user','content':'continue'}]
    with browser(['second answer', {'type':'request_error','error':'network failed'}, 'recovered', 'done']) as (client, commands):
        common = {'model':'gemini-4-carbon','prompt_cache_key':'large-history-retry'}
        first = client.post('/v1/responses', json={**common,'input':history}).json()
        history += first['output'] + [{'role':'user','content':'latest input'}]
        failed = client.post('/v1/responses', json={**common,'input':history,'stream':stream})
        assert (packets(failed.text)[-1]['type'] == 'response.failed') if stream else failed.status_code == 502
        recovered = client.post('/v1/responses', json={**common,'input':history}).json()
        assert recovered['status'] == 'completed'
        assert not commands[-1]['reset_chat']
        assert 'KEEP_FULL_SOURCE' not in commands[-1]['prompt']
        assert '[Retry Notice — Ignore Incomplete Previous Attempt]' in commands[-1]['prompt']
        assert 'latest input' in commands[-1]['prompt']
        history += recovered['output'] + [{'role':'user','content':'one more'}]
        assert client.post('/v1/responses', json={**common,'input':history}).json()['status'] == 'completed'
        assert 'Retry Notice' not in commands[-1]['prompt'] and not commands[-1]['reset_chat']


@pytest.mark.parametrize('stream', [False, True])
def test_http_large_history_double_failure_rebuilds_clean_session(stream):
    history = [{'role':'user','content':'KEEP_FULL_SOURCE\n' + 'x'*120000},
               {'role':'assistant','content':'recorded'}, {'role':'user','content':'continue'}]
    with browser([
        'second answer',
        {'type':'request_error','error':'network failed'},
        {'type':'request_error','error':'network failed again'},
        'recovered',
    ]) as (client, commands):
        common = {'model':'gemini-4-carbon','prompt_cache_key':'large-history-double-fail'}
        first = client.post('/v1/responses', json={**common,'input':history}).json()
        history += first['output'] + [{'role':'user','content':'latest input'}]
        failed1 = client.post('/v1/responses', json={**common,'input':history,'stream':stream})
        assert (packets(failed1.text)[-1]['type'] == 'response.failed') if stream else failed1.status_code == 502
        failed2 = client.post('/v1/responses', json={**common,'input':history,'stream':stream})
        assert (packets(failed2.text)[-1]['type'] == 'response.failed') if stream else failed2.status_code == 502
        assert not commands[-1]['reset_chat']
        recovered = client.post('/v1/responses', json={**common,'input':history}).json()
        assert recovered['status'] == 'completed'
        assert commands[-1]['reset_chat']
        assert 'KEEP_FULL_SOURCE' in commands[-1]['prompt']
        assert 'Retry Notice' not in commands[-1]['prompt']


def test_pre_submission_error_keeps_session_clean_without_retry_notice():
    with browser([
        'first answer',
        {'type':'request_error','submitted':False,'tab_reset':False,'error':'未找到已打开的标签页'},
        'recovered',
    ]) as (client, commands):
        common = {'model':'gemini-4-carbon','prompt_cache_key':'pre-submit-error'}
        first = client.post('/v1/responses', json={**common,'input':'hello'}).json()
        history = [{'role':'user','content':'hello'}] + first['output'] + [{'role':'user','content':'follow up'}]
        failed = client.post('/v1/responses', json={**common,'input':history})
        assert failed.status_code == 502
        recovered = client.post('/v1/responses', json={**common,'input':history}).json()
        assert recovered['status'] == 'completed'
        assert not commands[-1]['reset_chat']
        assert 'Retry Notice' not in commands[-1]['prompt']
        assert 'hello' not in commands[-1]['prompt']
        assert 'follow up' in commands[-1]['prompt']


def test_local_timeout_sync_preserves_remote_provider_and_global_budgets():
    original = '''model = "gpt-6.1-sol"
[model_providers.openai-custom]
base_url = "http://127.0.0.1:18001/v1"
wire_api = "responses"
stream_idle_timeout_ms = 300000
[model_providers."gemini-local"]
base_url = "http://127.0.0.1:8000/v1"
[model_providers.remote]
base_url = "https://example.com/v1"
stream_idle_timeout_ms = 1000
'''
    result = update_stream_timeouts(original)
    parsed = tomllib.loads(result)
    assert parsed['model'] == 'gpt-6.1-sol'
    assert 'model_auto_compact_token_limit' not in parsed
    assert parsed['model_providers']['openai-custom']['stream_idle_timeout_ms'] == 660000
    assert parsed['model_providers']['gemini-local']['stream_idle_timeout_ms'] == 660000
    assert parsed['model_providers']['remote']['stream_idle_timeout_ms'] == 1000
    assert update_stream_timeouts(result) == result

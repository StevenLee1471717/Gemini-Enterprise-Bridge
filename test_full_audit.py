"""Reproductions for the second gateway reliability audit."""
import asyncio
import json
import time

import pytest

from responses_api import ResponseStore, ResponsesError, prepare_request
from tool_calling import ToolStreamProcessor, parse_assistant_output
from test_responses import browser, packets


TOOLS = [{'type': 'function', 'function': {'name': 'functions.exec_command', 'parameters': {'type': 'object'}}}]


def call(cmd='verified'):
    return '[TOOL_CALL]' + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': cmd}}, ensure_ascii=False) + '[/TOOL_CALL]'


@pytest.mark.parametrize('closing', ['[/TOOL_CALL]', '</tool_call>'])
@pytest.mark.parametrize('width', [1, 73, 12000])
def test_protocol_markers_inside_arguments_are_literal(closing, width):
    cmd = 'printf \'[TOOL_CALL]{"name":"other","arguments":{}}' + closing + '\'\n# 中文 \\"'
    raw = call(cmd)
    if closing == '</tool_call>':
        raw = '<tool_call>' + raw[len('[TOOL_CALL]'):-len('[/TOOL_CALL]')] + '</tool_call>'
    processor = ToolStreamProcessor(TOOLS)
    emitted = []
    for start in range(0, len(raw), width):
        emitted.extend(processor.feed(raw[start:start + width]))
    tail, parsed = processor.finalize()
    assert not emitted + tail
    assert len(parsed.tool_calls) == 1
    assert json.loads(parsed.tool_calls[0]['function']['arguments']) == {'cmd': cmd}


def test_marker_in_first_call_does_not_execute_nested_or_drop_next():
    inner = call('inner')
    parsed = parse_assistant_output(call(inner) + call('second'), TOOLS)
    assert [json.loads(c['function']['arguments'])['cmd'] for c in parsed.tool_calls] == [inner, 'second']


def test_compaction_survives_reopen_and_normal_response_expiry(tmp_path):
    store = ResponseStore(directory=tmp_path, ttl_seconds=1)
    history = [{'role': 'user', 'content': 'checkpoint 42'}]
    store.put('bridge_compact_test', {}, history)
    store.put('resp_test', {'id': 'resp_test'}, history)
    store.close()
    reopened = ResponseStore(directory=tmp_path, ttl_seconds=1)
    assert reopened.get('resp_test')[1] == history
    with reopened.db:
        reopened.db.execute('UPDATE responses SET created = ?', (time.time() - 5,))
    with pytest.raises(ResponsesError):
        reopened.get('resp_test')
    request, _, _ = prepare_request({'input': [{'type': 'compaction', 'encrypted_content': 'bridge_compact_test'}, {'role':'user','content':'continue'}]}, reopened)
    assert request.messages[0].content == 'checkpoint 42'
    assert reopened.delete('bridge_compact_test')
    reopened.close()
    reopened = ResponseStore(directory=tmp_path)
    with pytest.raises(ResponsesError):
        reopened.get('bridge_compact_test')
    assert (tmp_path / 'responses.sqlite3').stat().st_mode & 0o777 == 0o600
    reopened.close()


def test_store_limits_fail_explicitly_and_preserve_compactions(tmp_path):
    store = ResponseStore(directory=tmp_path)
    store.MAX_ENTRY_BYTES = 1000
    store.MAX_TOTAL_BYTES = 1200
    history = [{'role':'user','content':'x' * 700}]
    store.put('bridge_compact_keep', {}, history)
    with pytest.raises(ResponsesError, match='容量'):
        store.put('bridge_compact_new', {}, history)
    assert store.get('bridge_compact_keep')[1] == history
    with pytest.raises(ResponsesError, match='过大'):
        store.put('oversize', {}, [{'role':'user','content':'x' * 2000}])
    store.close()


def test_repaired_preamble_matches_client_history():
    prefix = 'I will inspect the relevant files before proceeding. ' * 4
    request_tools = [{'type':'function', 'name':'functions.exec_command', 'parameters':{'type':'object'}}]
    with browser([prefix + '[TOOL_CALL]{', 'Repair finished. ' + call(), 'continued']) as (client, commands):
        payload = {'input': 'check', 'tools': request_tools, 'stream': True, 'prompt_cache_key':'repair-history-audit'}
        events = packets(client.post('/v1/responses', json=payload).text)
        response = events[-1]['response']
        assert response['status'] == 'completed'
        text = ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta')
        assert text == response['output'][0]['content'][0]['text']
        client.post('/v1/responses', json={**payload, 'stream':False,
            'input':[{'role':'user','content':'check'}] + response['output'] + [{'type':'function_call_output','call_id':response['output'][-1]['call_id'],'output':'verified'}]})
        assert commands[-1]['reset_chat'] is False
        assert 'I will inspect' not in commands[-1]['prompt']


def test_keepalive_during_buffered_work_and_cancellation():
    from streaming_utils import with_keepalive
    async def run():
        stopped = asyncio.Event()
        async def source():
            try:
                yield 'initial'
                await asyncio.Event().wait()
            finally:
                stopped.set()
        stream = with_keepalive(source(), interval=.01)
        assert await anext(stream) == 'initial'
        assert await asyncio.wait_for(anext(stream), .1) == ': keepalive\n\n'
        await stream.aclose()
        assert stopped.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())


def test_keepalive_preserves_errors_and_finite_output():
    from streaming_utils import with_keepalive
    async def run():
        async def source():
            yield 'one'
            await asyncio.sleep(.025)
            yield 'two'
            raise ValueError('upstream failed')
        output = []
        with pytest.raises(ValueError, match='upstream failed'):
            async for piece in with_keepalive(source(), interval=.01):
                output.append(piece)
        assert [p for p in output if not p.startswith(':')] == ['one', 'two']
        assert ': keepalive\n\n' in output
    asyncio.run(run())


def test_responses_image_wait_keeps_alive_and_cancels(monkeypatch):
    from fastapi.responses import StreamingResponse
    import responses_api
    from streaming_utils import with_keepalive
    monkeypatch.setattr(responses_api, 'with_keepalive', lambda s: with_keepalive(s, interval=.01))
    async def run():
        stopped = asyncio.Event()
        async def image_generator(prompt):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def upstream():
            yield 'data: ' + json.dumps({'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'name':'bridge_image_generation','arguments':'{"prompt":"test"}'}}]},'finish_reason':'tool_calls'}]}) + '\n\n'
            yield 'data: [DONE]\n\n'
        response = await responses_api.translate_response(StreamingResponse(upstream()), {}, 'gemini-4-carbon',
            {'bridge_image_generation':{'type':'image_generation', 'name':'bridge_image_generation'}}, [], ResponseStore(), image_generator)
        stream = response.body_iterator
        for _ in range(10):
            piece = await asyncio.wait_for(anext(stream), .2)
            if piece.startswith(':'):
                break
        else:
            pytest.fail('no heartbeat during image generation')
        await stream.aclose()
        assert stopped.is_set()
    asyncio.run(run())


def test_http_compaction_and_previous_id_survive_app_restart(tmp_path):
    settings = {'response_store_dir':str(tmp_path)}
    with browser(['Remember checkpoint 739.', 'ARGON reply', 'private'], **settings) as (client, _):
        compact = client.post('/v1/responses/compact', json={'input':'checkpoint 739'}).json()['output']
        previous = client.post('/v1/responses', json={'model':'gemini-4-argon', 'input':'remember Argon'}).json()
        private = client.post('/v1/responses', json={'input':'private','store':False}).json()
    with browser(['739', 'ARGON continued'], **settings) as (client, commands):
        assert client.post('/v1/responses', json={'input':compact + [{'role':'user','content':'continue'}]}).json()['status'] == 'completed'
        assert 'Remember checkpoint 739.' in commands[-1]['prompt']
        result = client.post('/v1/responses', json={'previous_response_id':previous['id'], 'input':'continue'}).json()
        assert result['model'] == 'gemini-4-argon'
        assert client.get('/v1/responses/' + previous['id']).status_code == 200
        assert client.get('/v1/responses/' + private['id']).status_code == 404
        assert client.delete('/v1/responses/' + previous['id']).status_code == 200


@pytest.mark.parametrize('stream', [False, True])
def test_failed_turn_invalidates_incremental_page_history(stream):
    with browser(['first answer', {'type':'request_error','error':'network failed'}, 'recovered']) as (client, commands):
        first = client.post('/v1/responses', json={'input':'first question', 'prompt_cache_key':'failure-recovery'}).json()
        history = [{'role':'user','content':'first question'}] + first['output'] + [{'role':'user','content':'next'}]
        failed = client.post('/v1/responses', json={'input':history, 'prompt_cache_key':'failure-recovery', 'stream':stream})
        if stream:
            assert packets(failed.text)[-1]['type'] == 'response.failed'
        else:
            assert failed.status_code == 502
        result = client.post('/v1/responses', json={'input':history, 'prompt_cache_key':'failure-recovery'}).json()
        assert result['status'] == 'completed'
        assert 'first question' in commands[-1]['prompt'], 'retry must reconstruct history after a partially submitted turn'


def test_buffered_tool_stream_keeps_client_alive(monkeypatch):
    from fastapi.testclient import TestClient
    from config import BridgeSettings
    from server import create_app
    from streaming_utils import with_keepalive
    monkeypatch.setattr('server.with_keepalive', lambda s: with_keepalive(s, interval=.01))
    app = create_app(BridgeSettings(auto_export_extension=False, auto_spawn_tabs=False))
    hub = app.state.hub
    hub.register_http_client('heartbeat-test', tab_id=1)
    hub._sync_client_tabs('heartbeat-test', [{'tab_id':1,'ready':True}])
    raw = call('x' * 3000)
    async def dispatch(**kwargs):
        for start in range(0, len(raw), 1000):
            yield raw[start:start + 1000]
            await asyncio.sleep(.025)
    hub.dispatch_stream_on_slot = dispatch
    with TestClient(app) as client:
        result = client.post('/v1/responses', json={'input':'large tool', 'stream':True,
            'tools':[{'type':'function','name':'functions.exec_command','parameters':{'type':'object'}}]})
        events = packets(result.text)
        assert ': keepalive\n\n' in result.text
        assert events[-1]['type'] == 'response.completed'
        assert not any(e['type'] == 'response.output_text.delta' for e in events)
        assert json.loads(events[-1]['response']['output'][0]['arguments'])['cmd'] == 'x' * 3000


def test_keepalive_cancellation_while_waiting_leaves_no_task():
    from streaming_utils import with_keepalive
    async def run():
        stopped = asyncio.Event()
        started = asyncio.Event()
        async def source():
            try:
                started.set()
                await asyncio.Event().wait()
                yield 'unreachable'
            finally:
                stopped.set()
        stream = with_keepalive(source(), interval=10)
        task = asyncio.create_task(anext(stream))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())

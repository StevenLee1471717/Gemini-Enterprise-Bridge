"""Codex wire contract and browser-boundary regression tests."""
import base64
from contextlib import contextmanager
from pathlib import Path
import json
import tempfile
import threading

from fastapi.testclient import TestClient
import pytest

from config import BridgeSettings
from extension_exporter import render_spark_icon_png
from responses_api import ResponseStore, ResponsesError, prepare_request
from server import create_app


@contextmanager
def browser(replies, **settings):
    with tempfile.TemporaryDirectory() as _tmp:
        defaults = dict(auto_export_extension=False, auto_spawn_tabs=False,
                        auto_reset_new_chat=False, generated_dir=_tmp,
                        response_store_dir=str(Path(_tmp) / "store"))
        defaults.update(settings)
        app = create_app(BridgeSettings(**defaults))
        with _browser_ctx(app, replies) as ctx:
            yield ctx


@contextmanager
def _browser_ctx(app, replies):
    commands, errors = [], []
    with TestClient(app) as client, client.websocket_connect('/ws/browser?client_id=test') as ws:
        ws.receive_json()
        ws.send_json({'type': 'tab_status', 'tab_id': 7, 'tab_url': 'https://vertexaisearch.cloud.google.com/u/1/home', 'ready': True})
        ws.receive_json()
        def worker():
            try:
                for reply in replies:
                    command = ws.receive_json()
                    while command.get('type') == 'cancel_request':
                        command = ws.receive_json()
                    commands.append(command)
                    event = {'type': 'request_complete', 'reply': reply} if isinstance(reply, str) else reply
                    ws.send_json({**event, 'request_id': command['request_id']})
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        yield client, commands
        thread.join(timeout=3)
        assert not thread.is_alive(), 'unconsumed browser commands'
        assert not errors


def packets(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]


def test_responses_text_stream_and_default_model():
    with browser(['hello', 'streamed']) as (client, commands):
        result = client.post('/v1/responses', json={'input': 'hello', 'store': False})
        assert result.status_code == 200
        response = result.json()
        assert response['status'] == 'completed'
        assert response['output'][0]['content'][0]['text'] == 'hello'
        stream = packets(client.post('/v1/responses', json={'input': 'next', 'stream': True}).text)
        assert stream[0]['type'] == 'response.created'
        assert stream[-1]['type'] == 'response.completed'
        assert [x['sequence_number'] for x in stream] == list(range(len(stream)))
        assert ''.join(x['delta'] for x in stream if x['type'] == 'response.output_text.delta') == 'streamed'


def test_custom_patch_namespace_and_tool_result():
    patch = '*** Begin Patch\n*** Add File: note.txt\n+hello\n*** End Patch'
    reply = '[TOOL_CALL]' + json.dumps({'name': 'functions.apply_patch', 'arguments': {'input': patch}}) + '[/TOOL_CALL]'
    tools = [{'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'custom', 'name': 'apply_patch', 'description': 'Apply patch'}]}]
    with browser([reply, 'verified']) as (client, commands):
        events = packets(client.post('/v1/responses', json={'input': 'write a note', 'tools': tools, 'stream': True}).text)
        call = events[-1]['response']['output'][0]
        assert call['type'] == 'custom_tool_call'
        assert call['namespace'] == 'functions'
        assert call['name'] == 'apply_patch' and call['input'] == patch
        assert any(e['type'] == 'response.custom_tool_call_input.done' and e['input'] == patch for e in events)
        result = client.post('/v1/responses', json={'tools': tools, 'previous_response_id': events[-1]['response']['id'],
            'input': [{'type': 'custom_tool_call_output', 'call_id': call['call_id'], 'output': 'Success. note.txt created.'}]})
        assert result.status_code == 200
        assert 'Success. note.txt created.' in commands[-1]['prompt']
        assert 'call_id="' + call['call_id'] + '"' in commands[-1]['prompt']


def test_parallel_function_calls_keep_ids_and_arguments():
    reply = ''.join('[TOOL_CALL]' + json.dumps({'name': 'read_file', 'arguments': {'path': path}}) + '[/TOOL_CALL]' for path in ['a', 'b'])
    with browser([reply]) as (client, _):
        events = packets(client.post('/v1/responses', json={'input': 'read both', 'stream': True,
            'tools': [{'type': 'function', 'name': 'read_file', 'parameters': {'type': 'object'}}]}).text)
        output = events[-1]['response']['output']
        assert [json.loads(x['arguments'])['path'] for x in output] == ['a', 'b']
        assert len({x['call_id'] for x in output}) == 2
        assert len([e for e in events if e['type'] == 'response.function_call_arguments.done']) == 2


def test_previous_response_retrieve_delete_and_store_false():
    with browser(['remember 42', '42', 'private']) as (client, commands):
        first = client.post('/v1/responses', json={'input': 'remember 42', 'instructions': 'OLD_INSTRUCTION'}).json()
        second = client.post('/v1/responses', json={'input': 'what number?', 'previous_response_id': first['id']}).json()
        assert 'remember 42' in commands[-1]['prompt']
        assert 'OLD_INSTRUCTION' not in commands[-1]['prompt']
        assert client.get('/v1/responses/' + second['id']).json()['status'] == 'completed'
        assert client.delete('/v1/responses/' + second['id']).json()['deleted'] is True
        assert client.get('/v1/responses/' + second['id']).status_code == 404
        private = client.post('/v1/responses', json={'input': 'private', 'store': False}).json()
        assert client.get('/v1/responses/' + private['id']).status_code == 404


def test_compaction_can_continue_and_expired_handle_is_explicit():
    with browser(['Goal: finish note.txt. Tests passed.', 'continued']) as (client, commands):
        compact = client.post('/v1/responses/compact', json={'input': 'We wrote note.txt and ran tests.'})
        assert compact.status_code == 200
        assert compact.json()['object'] == 'response.compaction'
        next_result = client.post('/v1/responses', json={'input': compact.json()['output'] + [{'role': 'user', 'content': 'continue'}]})
        assert next_result.status_code == 200
        assert 'Goal: finish note.txt. Tests passed.' in commands[-1]['prompt']
        missing = client.post('/v1/responses', json={'input': [{'type': 'compaction', 'encrypted_content': 'missing'}]})
        assert missing.status_code == 400


def test_browser_failure_never_emits_response_completed():
    with browser([{'type': 'request_error', 'error': 'Upload failed'}]) as (client, _):
        events = packets(client.post('/v1/responses', json={'input': 'test', 'stream': True}).text)
        assert events[-1]['type'] == 'response.failed'
        assert 'Upload failed' in events[-1]['response']['error']['message']
        assert not any(e['type'] == 'response.completed' for e in events)


def test_image_is_transported_and_not_replaced_with_a_text_only_reply():
    image = 'data:image/png;base64,' + base64.b64encode(render_spark_icon_png(16)).decode()
    with browser(['a star']) as (client, commands):
        result = client.post('/v1/responses', json={'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'What do you see?'}, {'type': 'input_image', 'image_url': image}]}]})
        assert result.status_code == 200
        assert commands[0]['attachments'][0]['data_url'] == image
        assert image not in commands[0]['prompt']
        assert '[Image attachment ' in commands[0]['prompt']
    with TestClient(create_app(BridgeSettings(auto_export_extension=False))) as client:
        for bad in ['https://example.com/image.png', 'data:image/png;base64,bm90LWEtcG5n']:
            response = client.post('/v1/responses', json={'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': bad}]}]})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'invalid_image'


def test_tool_output_images_and_unknown_tool_types():
    image = 'data:image/png;base64,' + base64.b64encode(render_spark_icon_png(16)).decode()
    request, mapping, _ = prepare_request({'input': [
        {'type': 'function_call', 'name': 'view_image', 'call_id': 'call_a', 'arguments': '{"path":"test.png"}'},
        {'type': 'function_call_output', 'call_id': 'call_a', 'output': [{'type': 'input_image', 'image_url': image}]}]}, ResponseStore())
    assert request.messages[-1].role == 'tool'
    assert request.messages[-1].content[0]['image_url']['url'] == image
    with pytest.raises(ResponsesError, match='服务端工具'):
        prepare_request({'input': 'search', 'tools': [{'type': 'file_search'}]}, ResponseStore())



@pytest.mark.parametrize('hosted_search', ['web_search', 'web_search_preview'])
def test_optional_hosted_search_keeps_local_tools_and_forced_search_fails(hosted_search):
    payload = {'input': 'hello', 'tools': [
        {'type': hosted_search},
        {'type': 'function', 'name': 'local_browser', 'parameters': {'type': 'object'}}]}
    request, mapping, _ = prepare_request(payload, ResponseStore())
    assert [tool['function']['name'] for tool in request.tools] == ['local_browser']
    assert list(mapping) == ['local_browser']
    with browser(['hello']) as (client, commands):
        response = client.post('/v1/responses', json=payload)
        assert response.status_code == 200
        assert 'local_browser' in commands[0]['prompt']
        assert hosted_search not in commands[0]['prompt']
    with pytest.raises(ResponsesError, match='tool_choice'):
        prepare_request({**payload, 'tool_choice': {'type': hosted_search}}, ResponseStore())
    with pytest.raises(ResponsesError, match='required'):
        prepare_request({'input': 'search', 'tools': [{'type': hosted_search}], 'tool_choice': 'required'}, ResponseStore())


def test_image_generation_returns_real_bytes_and_downloadable_file():
    raw = render_spark_icon_png(16)
    image = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    done = {'type': 'request_complete', 'reply': '', 'output_images': [{'data_url': image}]}
    with browser([done, done]) as (client, commands):
        result = client.post('/v1/images/generations', json={'prompt': 'a star'})
        assert result.status_code == 200
        assert base64.b64decode(result.json()['data'][0]['b64_json']) == raw
        assert commands[0]['image_generation'] is True
        assert commands[0]['reset_chat'] is True
        url_result = client.post('/v1/images/generations', json={'prompt': 'another star', 'response_format': 'url'})
        path = '/v1/images/files/' + url_result.json()['data'][0]['url'].split('/')[-1]
        assert client.get(path).content == raw
        assert client.get('/v1/images/files/missing.png').status_code == 404


def test_responses_native_image_tool_executes_in_bridge():
    raw = render_spark_icon_png(16)
    image = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    call = '[TOOL_CALL]{"name":"bridge_image_generation","arguments":{"prompt":"a star"}}[/TOOL_CALL]'
    with browser([call, {'type': 'request_complete', 'reply': '', 'output_images': [{'data_url': image}]}]) as (client, commands):
        events = packets(client.post('/v1/responses', json={'input': 'make a star', 'tools': [{'type': 'image_generation'}], 'stream': True}).text)
        assert events[-1]['type'] == 'response.completed'
        output = events[-1]['response']['output'][0]
        assert output['type'] == 'image_generation_call'
        assert base64.b64decode(output['result']) == raw
        assert '_prompt' not in output
        assert commands[-1]['image_generation'] is True


def test_image_generation_fails_when_no_image_was_returned():
    with browser(['Here is your image (no file).']) as (client, _):
        result = client.post('/v1/images/generations', json={'prompt': 'a star'})
        assert result.status_code == 502
        assert result.json()['error']['code'] == 'image_generation_failed'


def test_web_section_markers_are_removed_even_between_stream_chunks():
    from tool_calling import ToolStreamProcessor, parse_assistant_output
    source = 'hello [[section:Inspecting image]]world [ordinary]'
    for width in [1, 2, 5, 13, len(source)]:
        processor = ToolStreamProcessor([])
        output = []
        for start in range(0, len(source), width):
            output.extend(processor.feed(source[start:start+width]))
        tail, parsed = processor.finalize()
        output.extend(tail)
        assert ''.join(output) == 'hello \n\nworld [ordinary]'
        assert parsed.content == 'hello \n\nworld [ordinary]'
    assert parse_assistant_output('[[section:Call]]hi').content == 'hi'


def test_web_marker_filter_never_changes_tool_arguments_or_json_values():
    from tool_calling import ToolStreamProcessor
    tools = [{"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}}]
    original = {"name": "write_file", "arguments": {"content": "[[section:Literal]]\nkeep this"}}
    raw = "[[section:Calling tool]][TOOL_CALL]" + json.dumps(original) + "[/TOOL_CALL]"
    processor = ToolStreamProcessor(tools)
    for piece in raw:
        processor.feed(piece)
    _, parsed = processor.finalize()
    assert json.loads(parsed.tool_calls[0]["function"]["arguments"])["content"] == original["arguments"]["content"]
    json_processor = ToolStreamProcessor([], response_format={"type": "json_object"})
    json_processor.feed('{"content":"[[section:Literal]]"}')
    _, parsed_json = json_processor.finalize()
    assert json.loads(parsed_json.content)["content"] == "[[section:Literal]]"


def test_explicit_developer_messages_survive_previous_response():
    with browser(['first answer', 'second answer']) as (client, commands):
        first = client.post('/v1/responses', json={'instructions': 'one turn only', 'input': [
            {'role': 'developer', 'content': 'KEEP_DEVELOPER_RULE'}, {'role': 'user', 'content': 'hello'}]}).json()
        result = client.post('/v1/responses', json={'previous_response_id': first['id'], 'input': 'continue'})
        assert result.status_code == 200
        assert 'KEEP_DEVELOPER_RULE' in commands[-1]['prompt']
        assert 'one turn only' not in commands[-1]['prompt']


def test_token_count_and_malformed_requests_are_explicit():
    with TestClient(create_app(BridgeSettings(auto_export_extension=False))) as client:
        result = client.post('/v1/responses/input_tokens', json={'input': 'estimate this message'})
        assert result.status_code == 200 and result.json()['input_tokens'] > 0
        for payload in [
            {'input': 'x', 'tools': [{'type': 'namespace', 'name': 'bad', 'tools': [42]}]},
            {'input': 'x', 'tools': [{'type': 'function', 'name': 'bad', 'parameters': 1}]},
            {'input': 'x', 'tool_choice': 'invalid'},
            {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 7}]}]},
        ]:
            bad = client.post('/v1/responses', json=payload)
            assert bad.status_code == 400 and bad.json()['error']['type'] == 'invalid_request_error'

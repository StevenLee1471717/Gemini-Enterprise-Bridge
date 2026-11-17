"""Coding histories retain image references without uploading old screenshots again."""
import base64
import json
from pathlib import Path

import pytest

from extension_exporter import render_spark_icon_png
from test_responses import browser, packets

TOOLS = [{'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object'}}]
CALL = '```codex_tool_call\n' + json.dumps({'name': 'exec_command', 'arguments': {'cmd': 'ls /local/project'}}) + '\n```\n'


def image(size=16):
    return {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(render_spark_icon_png(size)).decode()}


def history():
    return [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Inspect the local project screenshots.'}] + [image(16+i) for i in range(8)]},
        {'role': 'assistant', 'content': 'The eight scenes have been inspected.'},
        {'role': 'user', 'content': 'Continue editing /local/project.'},
    ]


@pytest.mark.parametrize('stream', [False, True])
def test_repacked_coding_history_does_not_upload_eight_old_screenshots(stream):
    with browser([CALL], auto_reset_new_chat=True) as (client, commands):
        result = client.post('/v1/responses', json={'input': history(), 'tools': TOOLS, 'stream': stream})
        response = packets(result.text)[-1]['response'] if stream else result.json()
        assert response['status'] == 'completed'
        assert response['output'][0]['type'] == 'function_call'
        assert not commands[0].get('attachments')
        assert '[Image attachment ' not in commands[0]['prompt']
        assert commands[0]['prompt'].count('[Historical image reference ') == 8
        assert 'not attached' in commands[0]['prompt']
        assert 'eight scenes have been inspected' in commands[0]['prompt']
        assert '/local/project' in commands[0]['prompt']


@pytest.mark.parametrize('stream', [False, True])
def test_new_image_tool_result_uploads_once_and_preserves_incremental_history(stream):
    with browser([CALL, CALL, 'verified'], auto_reset_new_chat=True) as (client, commands):
        first = client.post('/v1/responses', json={'input': history(), 'tools': TOOLS}).json()
        call1 = first['output'][0]
        second = client.post('/v1/responses', json={
            'previous_response_id': first['id'], 'tools': TOOLS,
            'input': [{'type': 'function_call_output', 'call_id': call1['call_id'], 'output': [
                {'type': 'input_text', 'text': 'New screenshot from the local image viewer.'}, image(48)]}],
        }).json()
        assert len(commands[1]['attachments']) == 1
        assert not commands[1]['reset_chat']
        call2 = second['output'][0]
        third = client.post('/v1/responses', json={
            'previous_response_id': second['id'], 'tools': TOOLS, 'stream': stream,
            'input': [{'type': 'function_call_output', 'call_id': call2['call_id'], 'output': 'local check passed'}],
        })
        response = packets(third.text)[-1]['response'] if stream else third.json()
        assert response['status'] == 'completed'
        assert not commands[2].get('attachments')
        assert not commands[2]['reset_chat']
        assert 'local check passed' in commands[2]['prompt']
        assert 'eight scenes have been inspected' not in commands[2]['prompt']
        assert 'host-native' in commands[2]['prompt']
        assert 'client computer' in commands[2]['prompt']


def test_explicitly_supplied_old_image_is_uploaded_again():
    messages = history()
    messages[-1]['content'] = [{'type': 'input_text', 'text': 'Inspect this image again.'}, image()]
    with browser(['reinspected']) as (client, commands):
        assert client.post('/v1/responses', json={'input': messages, 'tools': TOOLS}).status_code == 200
        assert len(commands[0]['attachments']) == 1
        assert commands[0]['prompt'].count('[Historical image reference ') == 7


def test_normal_multimodal_chat_keeps_history_images():
    with browser(['compared']) as (client, commands):
        assert client.post('/v1/responses', json={'input': history()}).status_code == 200
        assert len(commands[0]['attachments']) == 8


def test_chat_completions_uses_the_same_image_policy():
    messages = history()
    messages[0]['content'] = [dict(p, type='image_url', image_url={'url': p['image_url']}) if p['type']=='input_image' else p for p in messages[0]['content']]
    with browser(['continued']) as (client, commands):
        result = client.post('/v1/chat/completions', json={'messages': messages, 'tools': [
            {'type': 'function', 'function': TOOLS[0]}]})
        assert result.status_code == 200
        assert not commands[0].get('attachments')
        assert commands[0]['prompt'].count('[Historical image reference ') == 8


def test_eight_fresh_images_are_still_uploaded_and_invalid_fresh_images_fail():
    first_user = history()[0]
    with browser(['inspected']) as (client, commands):
        response = client.post('/v1/responses', json={'input': [first_user], 'tools': TOOLS})
        assert response.status_code == 200
        assert len(commands[0]['attachments']) == 8
        assert '[Historical image reference ' not in commands[0]['prompt']
    with browser([]) as (client, commands):
        response = client.post('/v1/responses', json={'input': [{'role': 'user', 'content': [
            {'type': 'input_image', 'image_url': 'data:image/png;base64,bm90LWFuLWltYWdl'}]}], 'tools': TOOLS})
        assert response.status_code == 400
        assert not commands


def test_missing_historical_image_file_does_not_block_local_code_work():
    messages = history()
    messages[0]['content'] = [{'type': 'input_image', 'file_id': 'file_' + 'a'*32}]
    with browser(['continued']) as (client, commands):
        response = client.post('/v1/responses', json={'input': messages, 'tools': TOOLS})
        assert response.status_code == 200
        assert not commands[0].get('attachments')
        assert 'not attached' in commands[0]['prompt']


def test_cached_large_image_keeps_its_original_identity():
    from media_inputs import FileStore, compact_messages_for_store, extract_part_marker
    from schemas import ChatMessage
    import tempfile
    # The extra data is a valid PNG ancillary payload; the cache threshold is 128 KiB.
    raw = render_spark_icon_png(16) + b'padding' * 20000
    part = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(raw).decode()}}
    original = extract_part_marker(part)
    with tempfile.TemporaryDirectory() as directory:
        compacted = compact_messages_for_store([{'role': 'user', 'content': [part]}], FileStore(Path(directory)))
        cached = compacted[0]['content'][0]
        assert cached['image_url']['url'].startswith('file_')
        assert extract_part_marker(cached) == original
        assert ChatMessage(**compacted[0]).text_content() == original


def test_new_copy_of_cached_image_does_not_require_old_cache_file():
    from media_inputs import extract_part_marker
    part = image()
    marker = extract_part_marker(part)
    # Test the shared collector directly: public Responses normalization deliberately
    # omits internal cache metadata; ResponseStore reconstructs it as ChatMessage.
    from media_inputs import collect_attachments
    from schemas import ChatMessage
    stored = [ChatMessage(role='user', content=[{'type': 'image_url', 'image_url': {'url': 'file_'+'a'*32}, '_bridge_image_marker': marker}]),
              ChatMessage(role='assistant', content='inspected'), ChatMessage(role='user', content=[part])]
    attachments = collect_attachments(stored, pending_images_only=True)
    assert len(attachments) == 1 and attachments[0]['data_url'] == part['image_url']


def _long_tool_image_history(total_images: int = 12):
    items = [
        {'role': 'user', 'content': 'Refactor the 8 dungeon biome scenes and inspect screenshots.'},
    ]
    for idx in range(total_images):
        cid = f'call_img_{idx}'
        items.append({'type': 'function_call', 'call_id': cid, 'name': 'view_image', 'arguments': json.dumps({'path': f'/shot_{idx}.png'})})
        items.append({'type': 'function_call_output', 'call_id': cid, 'output': [
            {'type': 'input_text', 'text': f'Viewed shot {idx}'}, image(16 + idx),
        ]})
        if idx < total_images - 1:
            items.append({'role': 'assistant', 'content': f'Checked biome shot {idx}.'})
    return items


@pytest.mark.parametrize('stream', [False, True])
def test_inline_compaction_without_tools_skips_over_ten_historical_images(stream):
    items = _long_tool_image_history(14)
    items.append({
        'role': 'user',
        'content': 'You are performing a context checkpoint compaction. Create a handoff summary for another LLM that will resume the task.',
    })
    with browser(['Checkpoint summary: plague alchemica props updated.'], auto_reset_new_chat=True) as (client, commands):
        res = client.post('/v1/responses', json={'input': items, 'tools': [], 'stream': stream})
        assert res.status_code == 200
        payload = packets(res.text)[-1]['response'] if stream else res.json()
        assert payload['status'] == 'completed'
        assert not commands[0].get('attachments')
        assert '[Image attachment ' not in commands[0]['prompt']
        assert commands[0]['prompt'].count('[Historical image reference ') == 14


def test_compact_endpoint_skips_over_ten_historical_images():
    items = _long_tool_image_history(13)
    items.append({'role': 'assistant', 'content': 'Updated biome_props.js.'})
    with browser(['Compacted summary ready.'], auto_reset_new_chat=True) as (client, commands):
        res = client.post('/v1/responses/compact', json={'input': items})
        assert res.status_code == 200
        assert res.json()['object'] == 'response.compaction'
        assert not commands[0].get('attachments')
        assert '[Image attachment ' not in commands[0]['prompt']


def test_toolless_followup_on_tool_history_skips_over_ten_historical_images():
    items = _long_tool_image_history(12)
    items.append({'role': 'assistant', 'content': 'All 12 shots inspected.'})
    items.append({'role': 'user', 'content': 'Explain what changed in plain Chinese without running tools.'})
    with browser(['用中文总结完成。'], auto_reset_new_chat=True) as (client, commands):
        res = client.post('/v1/responses', json={'input': items, 'tools': []})
        assert res.status_code == 200
        assert not commands[0].get('attachments')
        assert commands[0]['prompt'].count('[Historical image reference ') == 12


def test_long_normal_multimodal_chat_incremental_and_rebuild_over_ten_images():
    # 1) Incremental continuation on the same browser tab across >10 cumulative images
    with browser(['seen 5', 'seen 10', 'seen 13'], auto_reset_new_chat=True) as (client, commands):
        t1 = [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Batch 1'}] + [image(16 + i) for i in range(5)]}]
        r1 = client.post('/v1/responses', json={'input': t1})
        assert r1.status_code == 200 and len(commands[0]['attachments']) == 5

        r2 = client.post('/v1/responses', json={
            'previous_response_id': r1.json()['id'],
            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Batch 2'}] + [image(24 + i) for i in range(5)]}],
        })
        assert r2.status_code == 200 and len(commands[1]['attachments']) == 5
        assert not commands[1]['reset_chat']

        r3 = client.post('/v1/responses', json={
            'previous_response_id': r2.json()['id'],
            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Batch 3'}] + [image(32 + i) for i in range(3)]}],
        })
        assert r3.status_code == 200 and len(commands[2]['attachments']) == 3
        assert not commands[2]['reset_chat']

    # 2) Fresh-tab rebuild of a normal chat with 12 old images + 2 new images caps to 10 without 400
    packed = [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Old batch'}] + [image(40 + i) for i in range(12)]},
        {'role': 'assistant', 'content': 'Reviewed 12 old photos.'},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Compare with 2 new photos'}] + [image(60 + i) for i in range(2)]},
    ]
    with browser(['rebuilt cleanly'], auto_reset_new_chat=True) as (client, commands):
        res = client.post('/v1/responses', json={'input': packed})
        assert res.status_code == 200
        atts = commands[0]['attachments']
        assert len(atts) == 10
        assert len({a['name'] for a in atts}) == 10
        assert commands[0]['prompt'].count('[Historical image reference ') == 4

    # 3) Single turn genuinely supplying 11 brand-new images still triggers the 10-attachment guardrail
    with browser([]) as (client, commands):
        over = [{'role': 'user', 'content': [{'type': 'input_text', 'text': '11 fresh images'}] + [image(70 + i) for i in range(11)]}]
        res = client.post('/v1/responses', json={'input': over, 'tools': TOOLS})
        assert res.status_code == 400
        assert '一次请求最多上传 10 个附件' in res.text
        assert not commands

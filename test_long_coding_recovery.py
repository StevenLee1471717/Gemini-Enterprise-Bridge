"""Regression of the 2026-11-13 large write / duplicate check / poisoned history failure."""
import json
import pytest
from tool_calling import BrowserTaskFailure, ToolProtocolError, ToolStreamProcessor, parse_assistant_output
from test_responses import browser, packets

TOOLS = [{'type':'function','name':'functions.exec_command','parameters':{'type':'object'}}]
PARSE_TOOLS = [{'type':'function','function':{'name':'functions.exec_command','parameters':{'type':'object'}}}]
FALLBACK = "I couldn't complete this request. If it needs a file, please attach it in the chat."


def call(cmd):
    return '```codex_tool_call\n' + json.dumps({'name':'functions.exec_command','arguments':{'cmd':cmd}}) + '\n```\n'


@pytest.mark.parametrize('stream', [False, True])
def test_long_write_is_never_dispatched_in_parallel_or_replayed_after_result(stream):
    cmd = "cat <<'EOF' > main.js\n" + ('// fixture data\n' * 1800) + 'EOF'
    bad = call(cmd) + call('ls -la main.js') + call('ls -la main.js')
    with browser([bad]) as (client,commands):
        result=client.post('/v1/responses',json={'model':'gemini-4-carbon','tools':TOOLS,'stream':stream,'input':'Write main.js then inspect its size.'})
        if stream:
            assert packets(result.text)[-1]['type']=='response.failed'
        else:
            assert result.status_code==502
        assert len(commands)==1
    with browser([call(cmd),call('ls -la main.js'),'verified']) as (client,commands):
        payload={'model':'gemini-4-carbon','tools':TOOLS,'stream':stream,'input':'Write main.js then inspect its size.'}
        def send(p):
            result=client.post('/v1/responses',json=p)
            return packets(result.text)[-1]['response'] if stream else result.json()
        first=send(payload)
        action=next(o for o in first['output'] if o['type']=='function_call')
        assert json.loads(action['arguments'])=={'cmd':cmd}
        result=send({**payload,'previous_response_id':first['id'],'input':[{'type':'function_call_output','call_id':action['call_id'],'output':'write successful; file size=27000'}]})
        check=next(o for o in result['output'] if o['type']=='function_call')
        assert json.loads(check['arguments'])=={'cmd':'ls -la main.js'}
        assert len(commands)==2 and not commands[-1]['reset_chat']
        assert cmd not in commands[-1]['prompt']
        end=send({**payload,'previous_response_id':result['id'],'input':[{'type':'function_call_output','call_id':check['call_id'],'output':'main.js 27000 bytes'}]})
        assert end['status']=='completed' and len(commands)==3


@pytest.mark.parametrize('stream', [False, True])
def test_cloud_fallback_fails_once_and_forces_clean_next_turn(stream):
    with browser([call('cat main.js'), FALLBACK, FALLBACK, 'recovered']) as (client, commands):
        payload={'model':'gemini-4-carbon','tools':TOOLS,'stream':stream,'prompt_cache_key':'fallback-regression','input':'Read main.js'}
        result=client.post('/v1/responses',json=payload)
        first=packets(result.text)[-1]['response'] if stream else result.json()
        call_id=first['output'][0]['call_id']
        result=client.post('/v1/responses',json={**payload,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'// existing content'}]})
        if stream:
            events=packets(result.text)
            assert events[-1]['type']=='response.failed'
            assert not any(e['type']=='response.completed' for e in events)
            assert FALLBACK not in ''.join(e.get('delta','') for e in events)
        else:
            assert result.status_code==502
        assert len(commands)==2  # no blind retry of an upstream failure
        assert client.get('/api/status').json()['hub']['total_errors']==1
        retry1=client.post('/v1/responses',json={**payload,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'// existing content'},{'role':'user','content':'Continue from actual results'}]})
        if stream:
            assert packets(retry1.text)[-1]['type']=='response.failed'
        else:
            assert retry1.status_code==502
        assert commands[-1]['reset_chat'] is False
        assert '[Retry Notice — Previous Cloud Fallback Reply Rejected; Retry In Current Session]' in commands[-1]['prompt']
        assert 'Read main.js' not in commands[-1]['prompt']
        retry2=client.post('/v1/responses',json={**payload,'stream':False,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'// existing content'},{'role':'user','content':'Continue from actual results'}]})
        assert retry2.json()['status']=='completed'
        assert commands[-1]['reset_chat'] is True
        assert 'Read main.js' in commands[-1]['prompt']
        assert '// existing content' in commands[-1]['prompt']


@pytest.mark.parametrize('stream', [False, True])
def test_protocol_format_error_rewrites_in_same_session_and_escalates_on_second_failure(stream):
    bad_tool = '```codex_tool_call\n{"name":"exec_command","arguments":{"cmd":"ls \\*"}\n```'
    with browser([call('cat main.js'), bad_tool, 'clean rewrite', 'next turn ok']) as (client, commands):
        payload={'model':'gemini-4-carbon','tools':TOOLS,'stream':stream,'prompt_cache_key':'fmt-rewrite','input':'Read main.js'}
        res1=client.post('/v1/responses',json=payload)
        first=packets(res1.text)[-1]['response'] if stream else res1.json()
        call_id=first['output'][0]['call_id']
        failed=client.post('/v1/responses',json={**payload,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'console.log(1)'}]})
        if stream:
            assert packets(failed.text)[-1]['type']=='response.failed'
        else:
            assert failed.status_code==502
        recovered_res=client.post('/v1/responses',json={**payload,'stream':False,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'console.log(1)'}]}).json()
        assert recovered_res['status']=='completed'
        assert commands[-1]['reset_chat'] is False
        assert '[Retry Notice — Previous Reply Had a Format Error; Please Rewrite]' in commands[-1]['prompt']
        assert 'Reason:' in commands[-1]['prompt']
        assert 'Read main.js' not in commands[-1]['prompt']
        followup=client.post('/v1/responses',json={**payload,'stream':False,'previous_response_id':recovered_res['id'],
            'input':[{'role':'user','content':'All good?'}]}).json()
        assert followup['status']=='completed'
        assert commands[-1]['reset_chat'] is False
        assert 'Retry Notice' not in commands[-1]['prompt']

    with browser([call('cat main.js'), bad_tool, bad_tool, 'rebuilt ok']) as (client, commands):
        payload={'model':'gemini-4-carbon','tools':TOOLS,'stream':stream,'prompt_cache_key':'fmt-double-fail','input':'Read main.js'}
        res1=client.post('/v1/responses',json=payload)
        first=packets(res1.text)[-1]['response'] if stream else res1.json()
        call_id=first['output'][0]['call_id']
        client.post('/v1/responses',json={**payload,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'console.log(1)'}]})
        client.post('/v1/responses',json={**payload,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'console.log(1)'}]})
        assert commands[-1]['reset_chat'] is False
        rebuilt=client.post('/v1/responses',json={**payload,'stream':False,'previous_response_id':first['id'],
            'input':[{'type':'function_call_output','call_id':call_id,'output':'console.log(1)'}]}).json()
        assert rebuilt['status']=='completed'
        assert commands[-1]['reset_chat'] is True
        assert 'Read main.js' in commands[-1]['prompt']
        assert 'console.log(1)' in commands[-1]['prompt']


def test_fallback_is_buffered_but_quoted_explanations_are_preserved():
    for width in [1, 13, 1000]:
        processor=ToolStreamProcessor(PARSE_TOOLS)
        for i in range(0,len(FALLBACK),width):
            assert processor.feed(FALLBACK[i:i+width])==[]
        with pytest.raises(BrowserTaskFailure): processor.finalize()
    text='The webpage reported: "'+FALLBACK+'" Please inspect the gateway.'
    processor=ToolStreamProcessor(PARSE_TOOLS)
    out=[]
    for c in text: out.extend(processor.feed(c))
    tail,_=processor.finalize()
    assert ''.join(out+tail)==text


def test_parallel_independent_reads_work_but_patches_and_checks_do_not_race():
    reads = call('ls -la main.js') + call('ls -la main.js') + call('cat other.js')
    parsed=parse_assistant_output(reads,PARSE_TOOLS)
    assert len(parsed.tool_calls)==2
    with pytest.raises(ToolProtocolError):
        parse_assistant_output(call('printf new > main.js')+call('cat main.js'),PARSE_TOOLS)


def test_adjoined_parallel_tool_fences_parse_cleanly():
    adjoined = (
        '```codex_tool_call\n'
        + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': 'cat a.js'}})
        + '\n``````codex_tool_call\n'
        + json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': 'cat b.js'}})
        + '\n```'
    )
    parsed = parse_assistant_output(adjoined, PARSE_TOOLS)
    assert len(parsed.tool_calls) == 2


@pytest.mark.parametrize('stream', [False, True])
def test_oversized_truncated_tool_json_seamlessly_continues_and_falls_back(stream):
    full_cmd = ''.join(f'step_{i:03d} = "segment_payload_{i:03d}"\n' for i in range(220))
    full_json = json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': full_cmd}}, ensure_ascii=False)
    cut_at = len(full_json) - 180
    part1 = '正在生成完整脚本。\n\n```codex_tool_call\n' + full_json[:cut_at]
    # Include a 24-char echoed overlap at the boundary to verify non-periodic overlap deduplication
    part2 = full_json[cut_at - 24 :] + '\n```'

    with browser([part1, part2]) as (client, commands):
        resp = client.post('/v1/responses', json={'model': 'gemini-4-carbon', 'tools': TOOLS, 'stream': stream, 'input': 'Generate big script'})
        final = packets(resp.text)[-1]['response'] if stream else resp.json()
        assert final['status'] == 'completed'
        fn_item = next(o for o in final['output'] if o['type'] == 'function_call')
        assert json.loads(fn_item['arguments']) == {'cmd': full_cmd}
        assert len(commands) == 2
        assert commands[1]['reset_chat'] is False
        assert '<<<TAIL_ANCHOR>>>' in commands[1]['prompt']

    # Zero-overlap continuation on repetitive/periodic lines must preserve every single line
    periodic_cmd = 'line = "omega_segment"\n' * 220
    periodic_json = json.dumps({'name': 'functions.exec_command', 'arguments': {'cmd': periodic_cmd}}, ensure_ascii=False)
    p_cut = len(periodic_json) - 180
    with browser(['```codex_tool_call\n' + periodic_json[:p_cut], periodic_json[p_cut:] + '\n```']) as (client, _):
        resp_p = client.post('/v1/responses', json={'model': 'gemini-4-carbon', 'tools': TOOLS, 'stream': stream, 'input': 'Periodic script'})
        final_p = packets(resp_p.text)[-1]['response'] if stream else resp_p.json()
        assert final_p['status'] == 'completed'
        fn_p = next(o for o in final_p['output'] if o['type'] == 'function_call')
        assert json.loads(fn_p['arguments']) == {'cmd': periodic_cmd}

    # Fallback to bounded Part-1 rewrite when continuation still fails to form valid JSON
    part1_broken = '正在生成脚本。\n\n```codex_tool_call\n' + full_json[:cut_at]
    bad_cont = 'still broken unclosed json {'
    bounded_part1 = call('printf part1_ok')
    with browser([part1_broken, bad_cont, bounded_part1]) as (client, commands):
        resp = client.post('/v1/responses', json={'model': 'gemini-4-carbon', 'tools': TOOLS, 'stream': stream, 'input': 'Generate huge script'})
        final = packets(resp.text)[-1]['response'] if stream else resp.json()
        assert final['status'] == 'completed'
        fn_item = next(o for o in final['output'] if o['type'] == 'function_call')
        assert json.loads(fn_item['arguments']) == {'cmd': 'printf part1_ok'}
        assert len(commands) == 3
        assert commands[2]['reset_chat'] is False


def test_ws_reconnect_preserves_inflight_and_avoids_false_503_plus_10x_subagent_spawn(tmp_path):
    import threading
    import time
    from fastapi.testclient import TestClient
    from config import BridgeSettings
    from server import create_app

    app = create_app(
        BridgeSettings(
            auto_export_extension=False,
            auto_spawn_tabs=True,
            max_parallel_tabs=10,
            generated_dir=str(tmp_path / 'gen'),
            response_store_dir=str(tmp_path / 'store'),
            session_archive_path=str(tmp_path / 'arch.sqlite3'),
        )
    )
    base_url = 'https://vertexaisearch.cloud.google.com/u/0/app/cid-10x'

    with TestClient(app) as client:
        # 1) In-flight request survives WS disconnect + concurrent request waits for WS reconnect instead of 503
        ws1_cm = client.websocket_connect('/ws/browser?client_id=ext-10x')
        ws1 = ws1_cm.__enter__()
        ws1.receive_json()  # welcome
        ws1.send_json({
            'type': 'tab_status',
            'capabilities': ['multi_tab', 'reset_tab', 'spawn_tab', 'open_session', 'session_busy_sync'],
            'max_parallel_tabs': 10,
            'tabs': [{'tab_id': 1, 'tab_url': base_url, 'ready': True}],
            'ready': True,
        })
        ws1.receive_json()  # ack

        inflight_cmd = {}
        cmd_received = threading.Event()

        def recv_on_ws1():
            while True:
                msg = ws1.receive_json()
                if msg.get('type') == 'execute_prompt':
                    inflight_cmd.update(msg)
                    cmd_received.set()
                    return

        t_recv = threading.Thread(target=recv_on_ws1, daemon=True)
        t_recv.start()

        results = {}
        def call_inflight():
            results['r1'] = client.post('/v1/chat/completions', json={
                'model': 'gemini-4-carbon',
                'messages': [{'role': 'user', 'content': 'INFLIGHT_SURVIVES_WS_DROP'}],
            })

        t_r1 = threading.Thread(target=call_inflight, daemon=True)
        t_r1.start()
        assert cmd_received.wait(timeout=3.0)

        # Simulate MV3 WS drop while r1 is generating!
        ws1_cm.__exit__(None, None, None)
        time.sleep(0.05)

        # Start a second request WHILE WS is disconnected — it must wait for reconnect (no 503!)
        def call_during_reconnect():
            results['r2'] = client.post('/v1/chat/completions', json={
                'model': 'gemini-4-carbon',
                'messages': [{'role': 'user', 'content': 'ARRIVED_DURING_WS_GAP'}],
            })

        t_r2 = threading.Thread(target=call_during_reconnect, daemon=True)
        t_r2.start()
        time.sleep(0.15)

        with client.websocket_connect('/ws/browser?client_id=ext-10x') as ws2:
            welcome2 = ws2.receive_json()
            assert welcome2['type'] == 'welcome'
            next_tab = 2
            spawned_tabs = []
            ack_received = threading.Event()
            ack_active_ids = []
            all_replied = threading.Event()
            replied_ws2 = 0
            ws_send_guard = threading.Lock()
            stop_drv = False

            def driver_ws2():
                nonlocal next_tab, replied_ws2
                while not stop_drv:
                    try:
                        msg = ws2.receive_json()
                    except Exception:
                        break
                    mtype = msg.get('type')
                    if mtype == 'ack':
                        ack_active_ids.extend(msg.get('active_request_ids') or [])
                        ack_received.set()
                    elif mtype == 'spawn_tab':
                        tid = next_tab
                        next_tab += 1
                        spawned_tabs.append(tid)
                        with ws_send_guard:
                            ws2.send_json({
                                'type': 'spawn_tab_result',
                                'spawn_id': msg['spawn_id'],
                                'ok': True,
                                'tab': {'tab_id': tid, 'tab_url': base_url, 'ready': True},
                            })
                    elif mtype == 'execute_prompt':
                        def reply_later(m=msg):
                            nonlocal replied_ws2
                            time.sleep(0.06)
                            with ws_send_guard:
                                ws2.send_json({
                                    'type': 'request_complete',
                                    'request_id': m['request_id'],
                                    'tab_id': m['tab_id'],
                                    'tab_url': f"{base_url}/r/session/vsid-{m['tab_id']}",
                                    'reply': f"OK_ON_TAB_{m['tab_id']}",
                                })
                                replied_ws2 += 1
                                if replied_ws2 >= 11:
                                    all_replied.set()
                        threading.Thread(target=reply_later, daemon=True).start()

            t_drv = threading.Thread(target=driver_ws2, daemon=True)
            t_drv.start()
            with ws_send_guard:
                ws2.send_json({
                    'type': 'tab_status',
                    'capabilities': ['multi_tab', 'reset_tab', 'spawn_tab', 'open_session', 'session_busy_sync'],
                    'max_parallel_tabs': 10,
                    'tabs': [{'tab_id': 1, 'tab_url': base_url, 'ready': True}],
                    'ready': True,
                })
            assert ack_received.wait(timeout=3.0)
            assert inflight_cmd['request_id'] in ack_active_ids

            # Deliver r1 completion via HTTP callback (just like background.js fallback)
            cb = client.post('/api/bridge/callback', json={
                'client_id': 'ext-10x',
                'tab_id': 1,
                'request_id': inflight_cmd['request_id'],
                'reply': 'RECOVERED_AFTER_WS_DROP',
            })
            assert cb.status_code == 200
            t_r1.join(timeout=3.0)
            t_r2.join(timeout=3.0)
            assert results['r1'].status_code == 200
            assert results['r1'].json()['choices'][0]['message']['content'] == 'RECOVERED_AFTER_WS_DROP'
            assert results['r2'].status_code == 200

            # 2) Fire 10 concurrent tool-enabled subagents sharing the same X-Session-Id -> all 10 run in parallel on distinct tabs!
            barrier_10 = threading.Barrier(10, timeout=5.0)
            ten_tabs = []
            ten_lock = threading.Lock()
            tool_def = [{'type': 'function', 'function': {'name': 'view', 'parameters': {'type': 'object'}}}]

            def run_lane(idx: int):
                barrier_10.wait()
                r = client.post(
                    '/v1/chat/completions',
                    headers={'X-Session-Id': 'shared-copilot-workspace'},
                    json={
                        'model': 'gemini-4-carbon',
                        'tools': tool_def,
                        'messages': [{'role': 'user', 'content': f'LANE_SUBAGENT_{idx}'}],
                    },
                )
                assert r.status_code == 200, r.text
                with ten_lock:
                    ten_tabs.append(r.headers.get('X-Bridge-Slot'))

            lanes = [threading.Thread(target=run_lane, args=(i,), daemon=True) for i in range(10)]
            for th in lanes:
                th.start()
            for th in lanes:
                th.join(timeout=5.0)
            assert len(set(ten_tabs)) == 10, f'Expected 10 distinct parallel slots, got {ten_tabs}'
            stop_drv = True

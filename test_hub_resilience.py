"""Regression checks for concurrency bounds, interrupted waits and session isolation."""
import asyncio
import contextlib

import pytest

from bridge_hub import BrowserBridgeHub, BridgeTimeoutError


def make_hub(cap=2, tabs=3):
    hub = BrowserBridgeHub(max_parallel_tabs=cap, auto_spawn_tabs=False)
    client = hub.register_http_client('test', tab_id=1)
    hub._sync_client_tabs('test', [{'tab_id': i, 'ready': True} for i in range(1, tabs + 1)])
    return hub, client


@pytest.mark.parametrize("cap,tabs", [(2, 5), (10, 12)])
def test_existing_tabs_cannot_exceed_global_cap(cap, tabs):
    async def run():
        hub, _ = make_hub(cap=cap, tabs=tabs)
        release = asyncio.Event()
        entered = []
        active = peak = 0
        async def work(i):
            nonlocal active, peak
            async with hub.acquire_slot(explicit_session_id=str(i)):
                active += 1
                peak = max(peak, active)
                entered.append(i)
                await release.wait()
                active -= 1
        tasks = [asyncio.create_task(work(i)) for i in range(tabs)]
        try:
            await asyncio.sleep(.03)
            assert len(entered) == cap
            assert hub.status_snapshot()['parallel_capacity'] == cap
            assert hub.status_snapshot()['queued_requests'] == tabs - cap
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert peak == cap
        assert all(not s.is_busy and s.waiting_count == 0 for s in hub._slots.values())
    asyncio.run(run())


def test_cancelled_capacity_wait_releases_slot_and_queue():
    async def run():
        hub, _ = make_hub(cap=1, tabs=2)
        entered = False
        async def blocked():
            nonlocal entered
            async with hub.acquire_slot(explicit_session_id='b'):
                entered = True
        async with hub.acquire_slot(explicit_session_id='a'):
            task = asyncio.create_task(blocked())
            await asyncio.sleep(.02)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert not entered
            assert hub._slots['test:2'].waiting_count == 0
            assert not hub._slots['test:2'].lock.locked()
        async with hub.acquire_slot(explicit_session_id='b', queue_timeout=.1):
            pass
    asyncio.run(run())


def test_cancelled_spawn_does_not_leave_phantom_waiter():
    async def run():
        hub, client = make_hub(cap=2, tabs=1)
        hub.auto_spawn_tabs = True
        client.transport = 'websocket'
        client.websocket = object()
        client.capabilities.add('spawn_tab')
        spawning = asyncio.Event()
        async def spawn(*_):
            spawning.set()
            await asyncio.Event().wait()
        hub._spawn_worker_tab = spawn
        async def blocked():
            async with hub.acquire_slot(explicit_session_id='b'):
                pass
        async with hub.acquire_slot(explicit_session_id='a'):
            task = asyncio.create_task(blocked())
            await spawning.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert client.spawning_count == 0
        assert sum(s.waiting_count for s in hub._slots.values()) == 0
    asyncio.run(run())


def test_old_session_binding_does_not_force_shared_page():
    async def run():
        hub, _ = make_hub(cap=2, tabs=2)
        hub._session_affinity.update(a='test:1', b='test:1')
        hub._slots['test:1'].pinned_session_id = 'b'
        async with hub.acquire_slot(explicit_session_id='a') as slot:
            assert slot.slot_key == 'test:2'
    asyncio.run(run())


def test_unchanged_progress_cannot_keep_request_alive_forever():
    async def run():
        hub, client = make_hub()
        async def receive():
            async with hub.acquire_slot() as slot:
                return await hub.dispatch_complete_on_slot(slot, 'test', request_timeout=.05)
        task = asyncio.create_task(receive())
        cmd = await client.command_queue.get()
        async def heartbeat():
            while True:
                await hub.handle_browser_event('test', {'type':'request_progress', 'request_id':cmd['request_id'], 'dom_len':0, 'net_len':0})
                await asyncio.sleep(.01)
        beats = asyncio.create_task(heartbeat())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(task), .3)
                assert False, 'stalled request should time out'
            except BridgeTimeoutError:
                pass
        finally:
            beats.cancel()
            task.cancel()
            await asyncio.gather(beats, task, return_exceptions=True)
    asyncio.run(run())


def test_reply_from_wrong_client_or_tab_is_ignored():
    async def run():
        hub, client = make_hub()
        async def receive():
            async with hub.acquire_slot() as slot:
                return await hub.dispatch_complete_on_slot(slot, 'test', request_timeout=.3)
        task = asyncio.create_task(receive())
        cmd = await client.command_queue.get()
        await hub.handle_browser_event('other', {'type':'request_complete', 'request_id':cmd['request_id'], 'reply':'wrong client'})
        await hub.handle_browser_event('test', {'type':'request_complete', 'request_id':cmd['request_id'], 'tab_id':99, 'reply':'wrong tab'})
        await hub.handle_browser_event('test', {'type':'request_complete', 'request_id':cmd['request_id'], 'tab_id':cmd['tab_id'], 'reply':'correct'})
        assert await task == 'correct'
    asyncio.run(run())


def test_capacity_queue_timeout_releases_every_resource():
    async def run():
        hub, _ = make_hub(cap=1, tabs=2)
        async with hub.acquire_slot(explicit_session_id='a'):
            try:
                async with hub.acquire_slot(explicit_session_id='b', queue_timeout=.02):
                    assert False
            except BridgeTimeoutError:
                pass
            assert not hub._slots['test:2'].is_busy
            assert hub.status_snapshot()['queued_requests'] == 0
        async with hub.acquire_slot(explicit_session_id='b', queue_timeout=.1):
            pass
    asyncio.run(run())


def test_page_closed_while_waiting_is_not_dispatched():
    async def run():
        hub, _ = make_hub(cap=1, tabs=1)
        entered = False
        async def waiting():
            nonlocal entered
            from bridge_hub import BridgeUnavailableError
            try:
                async with hub.acquire_slot(explicit_session_id='a'):
                    entered = True
            except BridgeUnavailableError:
                pass
        async with hub.acquire_slot(explicit_session_id='a') as slot:
            task = asyncio.create_task(waiting())
            await asyncio.sleep(.01)
            hub._remove_slot(slot.slot_key)
        await task
        assert not entered
        assert slot.waiting_count == 0
        assert not slot.lock.locked()
    asyncio.run(run())


def test_cancelled_page_wait_does_not_clear_active_request():
    async def run():
        hub, _ = make_hub(cap=1, tabs=1)
        async def waiting():
            async with hub.acquire_slot(explicit_session_id='a'):
                assert False
        async with hub.acquire_slot(explicit_session_id='a') as slot:
            slot.in_flight_request_id = 'still-running'
            task = asyncio.create_task(waiting())
            await asyncio.sleep(.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert slot.in_flight_request_id == 'still-running'
            assert slot.lock.locked()
            assert slot.waiting_count == 0
    asyncio.run(run())

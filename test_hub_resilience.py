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
            with pytest.raises(BridgeTimeoutError):
                async with hub.acquire_slot(explicit_session_id='c', queue_timeout=.02):
                    pytest.fail('a cancelled waiter must not give away the active permit')
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


def test_verified_thread_queues_on_same_page_with_unlimited_capacity():
    async def run():
        hub, _ = make_hub(cap=0, tabs=3)
        async with hub.acquire_slot(explicit_session_id='other', strict_session_affinity=True) as other:
            pass

        async def continuation():
            async with hub.acquire_slot(
                explicit_session_id='thread', strict_session_affinity=True,
                matching_slot_keys=[other.slot_key], avoid_affinity_slot=True,
            ) as slot:
                return slot.slot_key

        async with hub.acquire_slot(explicit_session_id='thread', strict_session_affinity=True) as first:
            task = asyncio.create_task(continuation())
            try:
                async with hub.acquire_slot(explicit_session_id='independent', strict_session_affinity=True) as independent:
                    assert independent.slot_key not in (first.slot_key, other.slot_key)
                    assert not task.done()
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        assert await asyncio.wait_for(task, 1) == first.slot_key
        assert await continuation() == first.slot_key
        assert sum(s.pinned_session_id == 'thread' for s in hub._slots.values()) == 1
    asyncio.run(run())


def test_verified_thread_first_requests_share_one_spawn():
    async def run():
        hub, client = make_hub(cap=0, tabs=1)
        hub.auto_spawn_tabs = True
        client.transport = 'websocket'
        client.websocket = object()
        client.capabilities.add('spawn_tab')
        hub.restore_slot_affinity('test:1', 'occupied')
        spawning = asyncio.Event()
        ready = asyncio.Event()
        spawn_count = 0

        async def spawn(*_):
            nonlocal spawn_count
            spawn_count += 1
            spawning.set()
            await ready.wait()
            return hub._upsert_single_slot('test', 2)

        async def request():
            async with hub.acquire_slot(explicit_session_id='thread', strict_session_affinity=True) as slot:
                return slot.slot_key

        hub._spawn_worker_tab = spawn
        first = asyncio.create_task(request())
        await spawning.wait()
        second = asyncio.create_task(request())
        await asyncio.sleep(0)
        ready.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 1) == ['test:2', 'test:2']
        assert spawn_count == 1
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


@pytest.mark.parametrize("final", ["rewritten answer", ""])
def test_completion_must_match_original_chunks_and_failure_cancels(final):
    from bridge_hub import BridgeExecutionError
    async def run():
        hub, client = make_hub()
        async def receive():
            async with hub.acquire_slot() as slot:
                return await hub.dispatch_complete_on_slot(slot, "test", request_timeout=.3)
        task=asyncio.create_task(receive())
        command=await client.command_queue.get()
        await hub.handle_browser_event('test',{'type':'stream_chunk','request_id':command['request_id'],'delta':'original answer'})
        await hub.handle_browser_event('test',{'type':'request_complete','request_id':command['request_id'],'reply':final,'source':'network_stream'})
        with pytest.raises(BridgeExecutionError,match='不一致'):
            await task
        assert hub.status_snapshot()['total_errors']==1
        assert not hub._pending
        assert not any(s.in_flight_request_id for s in hub._slots.values())
        cancel=await client.command_queue.get()
        assert cancel['type']=='cancel_request'
        assert 'source=network_stream' in hub._recent_logs[-1]['message']
    asyncio.run(run())


def test_custom_parallel_tabs_wakes_waiters_and_rejects_unlimited_zero():
    async def run():
        # Legacy 0 falls back to default 10 rather than unlimited
        hub, _ = make_hub(cap=0, tabs=14)
        release = asyncio.Event()
        entered = []

        async def work(i):
            async with hub.acquire_slot(explicit_session_id=f"u-{i}"):
                entered.append(i)
                await release.wait()

        tasks = [asyncio.create_task(work(i)) for i in range(14)]
        try:
            await asyncio.sleep(0.03)
            snap = hub.status_snapshot()
            assert len(entered) == 10
            assert snap["max_parallel_tabs"] == 10
            assert snap["unlimited_parallel_tabs"] is False
            assert snap["parallel_capacity"] == 10
            assert snap["queued_requests"] == 4
        finally:
            release.set()
            await asyncio.gather(*tasks)

        # Switching from cap=2 -> custom cap=5 at runtime immediately wakes queued tasks
        hub2, _ = make_hub(cap=2, tabs=5)
        release2 = asyncio.Event()
        entered2 = []

        async def work2(i):
            async with hub2.acquire_slot(explicit_session_id=f"w-{i}"):
                entered2.append(i)
                await release2.wait()

        tasks2 = [asyncio.create_task(work2(i)) for i in range(5)]
        try:
            await asyncio.sleep(0.03)
            assert len(entered2) == 2
            assert hub2.status_snapshot()["queued_requests"] == 3
            await hub2.set_max_parallel_tabs(5)
            await asyncio.sleep(0.03)
            assert len(entered2) == 5
            assert hub2.status_snapshot()["queued_requests"] == 0
        finally:
            release2.set()
            await asyncio.gather(*tasks2)

        with pytest.raises(ValueError):
            await hub2.set_max_parallel_tabs(0)

    asyncio.run(run())

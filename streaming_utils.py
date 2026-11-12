"""Keep client streams alive while upstream work is buffered or waiting."""
import asyncio


async def with_keepalive(stream, interval=14.5):
    pending = None
    try:
        while True:
            pending = asyncio.create_task(anext(stream))
            while not (await asyncio.wait({pending}, timeout=interval))[0]:
                yield ': keepalive\n\n'
            try:
                piece = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield piece
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()

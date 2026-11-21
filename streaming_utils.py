"""Keep client streams alive while upstream work is buffered or waiting."""
import asyncio

from anyio import CancelScope
from fastapi.responses import StreamingResponse


class ClosingStreamingResponse(StreamingResponse):
    """Close the owned async stream even when the client disconnects between frames."""

    def __init__(self, content, *, on_close=None, **kwargs):
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def close(self):
        with CancelScope(shield=True):
            try:
                await self.body_iterator.aclose()
            finally:
                if self._on_close is not None:
                    on_close, self._on_close = self._on_close, None
                    await on_close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close()


async def with_keepalive(stream, interval=12.0, frame=': keepalive\n\n'):
    pending = None
    try:
        while True:
            pending = asyncio.create_task(anext(stream))
            while not (await asyncio.wait({pending}, timeout=interval))[0]:
                yield frame() if callable(frame) else frame
            try:
                piece = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield piece
    finally:
        with CancelScope(shield=True):
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()

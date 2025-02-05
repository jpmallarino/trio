import errno
import select

import os
import sys
from typing import Any
from typing import Tuple

import pytest

from .._core._tests.tutil import gc_collect_harder
from .. import _core, move_on_after
from ..testing import wait_all_tasks_blocked, check_one_way_stream

if sys.platform == "win32":
    from .._windows_pipes import PipeSendStream, PipeReceiveStream
    from .._core._windows_cffi import _handle, kernel32
    from asyncio.windows_utils import pipe
else:
    pytestmark = pytest.mark.skip(reason="windows only")
    pipe: Any = None
    PipeSendStream: Any = None
    PipeReceiveStream: Any = None


async def make_pipe() -> Tuple[PipeSendStream, PipeReceiveStream]:
    """Makes a new pair of pipes."""
    (r, w) = pipe()
    return PipeSendStream(w), PipeReceiveStream(r)


async def test_pipe_typecheck():
    with pytest.raises(TypeError):
        PipeSendStream(1.0)
    with pytest.raises(TypeError):
        PipeReceiveStream(None)


async def test_pipe_error_on_close():
    # Make sure we correctly handle a failure from kernel32.CloseHandle
    r, w = pipe()

    send_stream = PipeSendStream(w)
    receive_stream = PipeReceiveStream(r)

    assert kernel32.CloseHandle(_handle(r))
    assert kernel32.CloseHandle(_handle(w))

    with pytest.raises(OSError):
        await send_stream.aclose()
    with pytest.raises(OSError):
        await receive_stream.aclose()


async def test_pipes_combined():
    write, read = await make_pipe()
    count = 2**20
    replicas = 3

    async def sender():
        async with write:
            big = bytearray(count)
            for _ in range(replicas):
                await write.send_all(big)

    async def reader():
        async with read:
            await wait_all_tasks_blocked()
            total_received = 0
            while True:
                # 5000 is chosen because it doesn't evenly divide 2**20
                received = len(await read.receive_some(5000))
                if not received:
                    break
                total_received += received

            assert total_received == count * replicas

    async with _core.open_nursery() as n:
        n.start_soon(sender)
        n.start_soon(reader)


async def test_async_with():
    w, r = await make_pipe()
    async with w, r:
        pass

    with pytest.raises(_core.ClosedResourceError):
        await w.send_all(b"")
    with pytest.raises(_core.ClosedResourceError):
        await r.receive_some(10)


async def test_close_during_write():
    w, r = await make_pipe()
    async with _core.open_nursery() as nursery:

        async def write_forever():
            with pytest.raises(_core.ClosedResourceError) as excinfo:
                while True:
                    await w.send_all(b"x" * 4096)
            assert "another task" in str(excinfo.value)

        nursery.start_soon(write_forever)
        await wait_all_tasks_blocked(0.1)
        await w.aclose()


async def test_pipe_fully():
    # passing make_clogged_pipe tests wait_send_all_might_not_block, and we
    # can't implement that on Windows
    await check_one_way_stream(make_pipe, None)

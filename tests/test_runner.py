import asyncio
import errno
import json

import pytest

import open_terminal.utils.runner as runner_module
from open_terminal.utils.runner import PtyRunner


class AsyncLog:
    def __init__(self):
        self.lines = []

    async def write(self, value):
        self.lines.append(value)


def test_pty_runner_reads_buffered_output_after_eio(monkeypatch):
    runner = PtyRunner.__new__(PtyRunner)
    runner._master_fd = 123
    reads = iter(
        [
            OSError(errno.EIO, "input/output error"),
            b"buffered output",
            OSError(errno.EIO, "input/output error"),
        ]
    )

    def fake_read(_fd, _size):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("open_terminal.utils.runner.os.read", fake_read)
    log = AsyncLog()

    asyncio.run(runner.read_output(log))

    assert [json.loads(line)["data"] for line in log.lines] == ["buffered output"]


def test_pty_runner_does_not_drain_after_other_os_error(monkeypatch):
    runner = PtyRunner.__new__(PtyRunner)
    runner._master_fd = 123

    def fake_read(_fd, _size):
        raise OSError(errno.EBADF, "bad file descriptor")

    monkeypatch.setattr("open_terminal.utils.runner.os.read", fake_read)
    log = AsyncLog()

    asyncio.run(runner.read_output(log))

    assert log.lines == []


@pytest.mark.skipif(not runner_module._PTY_AVAILABLE, reason="Unix PTY required")
def test_pty_runner_captures_output_from_fast_command():
    async def run():
        runner = PtyRunner("printf hello", None, None)
        log = AsyncLog()
        try:
            await asyncio.wait_for(
                asyncio.gather(runner.read_output(log), runner.wait()),
                timeout=5,
            )
        finally:
            runner.close()
        return [json.loads(line)["data"] for line in log.lines]

    assert "".join(asyncio.run(run())) == "hello"

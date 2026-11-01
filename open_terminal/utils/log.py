"""Bounded process logs, cursor reads, and failure-safe process finalization."""

import asyncio
from collections import deque
import json
import logging
import os
import shutil
import time
import tempfile
from typing import Optional

import aiofiles
import aiofiles.os

from open_terminal.env import MAX_PROCESS_LOG_SIZE, LOG_FLUSH_INTERVAL, LOG_FLUSH_BUFFER
from open_terminal.utils.limits import CHUNK_SIZE, run_io

log = logging.getLogger(__name__)


def _records(stream):
    for line in stream:
        try:
            yield json.loads(line)
        except (ValueError, UnicodeError):
            continue


def _rotate_log(path: str) -> int:
    temporary = path + ".rotating"
    try:
        with open(path, "rb") as source:
            split = sum(1 for _ in source) // 2
            source.seek(0)
            offset = 0
            for _ in range(split):
                record = json.loads(source.readline())
                if record.get("type") == "log_rotated":
                    offset = record.get("offset", 0)
                elif record.get("type") in ("stdout", "stderr", "output"):
                    offset += 1
            kept_size = os.fstat(source.fileno()).st_size - source.tell()
            with open(temporary, "wb") as target:
                target.write((json.dumps({"type": "log_rotated", "offset": offset, "ts": time.time()}) + "\n").encode())
                shutil.copyfileobj(source, target, CHUNK_SIZE)
        os.replace(temporary, path)
        return kept_size
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class BoundedLogWriter:
    """Rotate on disk; disable logging on I/O failure while draining the child."""

    def __init__(self, file, log_path: str, *, flush_interval: float = 0, flush_buffer: int = 0, on_flush=None):
        self._file = file
        self._log_path = log_path
        self._bytes_written = 0
        self.rotated = False
        self._flush_interval = flush_interval
        self._flush_buffer = flush_buffer
        self._unflushed = 0
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()
        self._on_flush = on_flush
        self._flush_task = None

    async def _disable(self, exc):
        log.warning("Disabling process log %s: %s", self._log_path, exc)
        await self.close()

    async def write(self, data: str) -> None:
        async with self._lock:
            if self._file is None:
                return
            try:
                size = len(data.encode("utf-8", errors="replace"))
                if self._bytes_written + size > MAX_PROCESS_LOG_SIZE:
                    await self._rotate()
                await self._file.write(data)
                self._bytes_written += size
                self._unflushed += size
                if (self._flush_interval <= 0
                    or time.monotonic() - self._last_flush >= self._flush_interval
                    or self._flush_buffer > 0 and self._unflushed >= self._flush_buffer):
                    await self.flush()
                elif self._flush_task is None:
                    self._flush_task = asyncio.create_task(self._flush_pending())
            except (OSError, ValueError) as exc:
                await self._disable(exc)

    async def flush(self) -> None:
        task, self._flush_task = self._flush_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if self._file is not None:
            try:
                await self._file.flush()
                self._unflushed = 0
                self._last_flush = time.monotonic()
                if self._on_flush:
                    self._on_flush()
            except (OSError, ValueError) as exc:
                await self._disable(exc)

    async def close(self):
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        file, self._file = self._file, None
        if file is not None:
            try:
                await file.close()
            except (OSError, ValueError):
                pass

    async def _flush_pending(self):
        # A quiet command still needs to publish its last buffered chunk.
        await asyncio.sleep(self._flush_interval)
        async with self._lock:
            await self.flush()

    async def _rotate(self):
        await self._file.flush()
        await self._file.close()
        self._file = None
        self._bytes_written = await run_io(_rotate_log, self._log_path)
        self._file = await aiofiles.open(self._log_path, "a", encoding="utf-8")
        self.rotated = True


async def log_process(background_process, on_finish=None) -> None:
    writer = None
    runner = background_process.runner
    try:
        if background_process.log_path:
            try:
                await aiofiles.os.makedirs(os.path.dirname(background_process.log_path), exist_ok=True)
                file = await aiofiles.open(background_process.log_path, "a", encoding="utf-8")
                writer = BoundedLogWriter(file, background_process.log_path,
                    flush_interval=LOG_FLUSH_INTERVAL, flush_buffer=LOG_FLUSH_BUFFER,
                    on_flush=background_process.notify_output)
                await file.write(json.dumps({"type": "start", "command": background_process.command, "pid": runner.pid, "ts": time.time()}) + "\n")
                await writer.flush()
            except OSError as exc:
                log.warning("Cannot open process log: %s", exc)
        await runner.read_output(writer)
        background_process.exit_code = await runner.wait()
    except asyncio.CancelledError:
        background_process.status = "killed"
        runner.kill(force=True)
        raise
    except Exception as exc:
        runner.kill(force=True)
        log.warning("Command %s failed: %s", background_process.id, exc)
    finally:
        try:
            background_process.exit_code = await runner.wait()
        finally:
            runner.close()
            if background_process.status == "running":
                background_process.status = "done"
            if writer:
                try:
                    await writer.write(json.dumps({"type": "end", "exit_code": background_process.exit_code, "log_rotated": writer.rotated, "ts": time.time()}) + "\n")
                    await writer.flush()
                finally:
                    await writer.close()
            background_process.finished_at = time.time()
            background_process.notify_output()
            if on_finish:
                on_finish()


class LogReader:
    """Keep a byte position for live reads; retain absolute cursors across rotation."""

    def __init__(self, path, offset=0, tail=None):
        self.path = path
        self.offset = offset
        self.tail = tail
        self.identity = None
        self.position = 0
        self.total = 0

    def _read(self):
        selected = deque(maxlen=self.tail)
        available = 0
        truncated = False
        try:
            source = open(self.path, "rb") if self.path else None
        except FileNotFoundError:
            source = None
        if source is None:
            return [], self.offset, False
        with source:
            stat = os.fstat(source.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.position:
                self.position = self.total = 0
            source.seek(self.position)
            while True:
                position = source.tell()
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    # A concurrent writer may not have finished its last record yet.
                    self.position = position
                    break
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                kind = record.get("type")
                if kind == "log_rotated":
                    self.total = record.get("offset", 0)
                    truncated = self.offset < self.total
                elif kind in ("stdout", "stderr", "output"):
                    if self.total >= self.offset:
                        selected.append({"type": kind, "data": record["data"]})
                        available += 1
                    self.total += 1
            self.identity = identity
            self.offset = self.total
        return list(selected), self.offset, truncated or available > len(selected)

    async def read(self):
        return await run_io(self._read)


async def read_log(log_path: Optional[str], offset: int = 0, tail: Optional[int] = None) -> tuple[list[dict], int, bool]:
    return await LogReader(log_path, offset, tail).read()


def _build_log_response(path, metadata, offset, tail):
    """Serialize complete output to disk without retaining a list of all entries."""
    from open_terminal.utils.fs import TemporaryFileResponse

    fd, temporary = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(metadata, ensure_ascii=False)[:-1] + ',"output":[')
            total = available = 0
            truncated = False
            try:
                source = open(path, "rb") if path else None
            except FileNotFoundError:
                source = None
            if source is not None:
                with source:
                    # Hold this generation's descriptor across both passes. A
                    # rotation replaces the pathname but cannot change our data.
                    end = os.fstat(source.fileno()).st_size
                    def records():
                        while source.tell() < end:
                            line = source.readline(end - source.tell())
                            try:
                                yield json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                    for record in records():
                        kind = record.get("type")
                        if kind == "log_rotated":
                            total = record.get("offset", 0)
                            truncated = offset < total
                        elif kind in ("stdout", "stderr", "output"):
                            available += total >= offset
                            total += 1
                    skip = max(0, available - tail) if tail is not None else 0
                    truncated |= skip > 0
                    source.seek(0)
                    index = 0
                    separator = ""
                    for record in records():
                        kind = record.get("type")
                        if kind == "log_rotated":
                            index = record.get("offset", 0)
                        elif kind in ("stdout", "stderr", "output"):
                            if index >= offset:
                                if skip:
                                    skip -= 1
                                else:
                                    output.write(separator)
                                    json.dump({"type": kind, "data": record["data"]}, output, ensure_ascii=False)
                                    separator = ","
                            index += 1
            output.write('],"next_offset":' + str(total) + ',"truncated":' + json.dumps(truncated) + '}')
        return TemporaryFileResponse(temporary, media_type="application/json")
    except BaseException:
        os.unlink(temporary)
        raise


async def log_response(path, metadata, offset=0, tail=None):
    task = asyncio.create_task(asyncio.to_thread(_build_log_response, path, metadata, offset, tail))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            response = await task
        except Exception:
            pass
        else:
            os.unlink(response.path)
        raise

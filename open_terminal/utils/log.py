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

from open_terminal.env import MAX_PROCESS_LOG_SIZE, LOG_FLUSH_INTERVAL, LOG_FLUSH_BUFFER, MAX_TOOL_OUTPUT_SIZE
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

    def __init__(self, file, log_path: str, *, flush_interval: float = 0, flush_buffer: int = 0):
        self._file = file
        self._log_path = log_path
        self._bytes_written = 0
        self.rotated = False
        self._flush_interval = flush_interval
        self._flush_buffer = flush_buffer
        self._unflushed = 0
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

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
            except (OSError, ValueError) as exc:
                await self._disable(exc)

    async def flush(self) -> None:
        if self._file is not None:
            try:
                await self._file.flush()
                self._unflushed = 0
                self._last_flush = time.monotonic()
            except (OSError, ValueError) as exc:
                await self._disable(exc)

    async def close(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                await file.close()
            except (OSError, ValueError):
                pass

    async def _rotate(self):
        await self._file.flush()
        await self._file.close()
        self._file = None
        self._bytes_written = await run_io(_rotate_log, self._log_path)
        self._file = await aiofiles.open(self._log_path, "a", encoding="utf-8")
        self.rotated = True


async def log_process(background_process) -> None:
    writer = None
    runner = background_process.runner
    try:
        if background_process.log_path:
            try:
                await aiofiles.os.makedirs(os.path.dirname(background_process.log_path), exist_ok=True)
                file = await aiofiles.open(background_process.log_path, "a", encoding="utf-8")
                writer = BoundedLogWriter(file, background_process.log_path,
                    flush_interval=LOG_FLUSH_INTERVAL, flush_buffer=LOG_FLUSH_BUFFER)
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
            background_process.finished_at = time.time()
            if writer:
                try:
                    await writer.write(json.dumps({"type": "end", "exit_code": background_process.exit_code, "log_rotated": writer.rotated, "ts": time.time()}) + "\n")
                    await writer.flush()
                finally:
                    await writer.close()


def _read_log(path, offset, tail):
    selected = deque(maxlen=tail)
    total = available = 0
    truncated = False
    try:
        source = open(path, "rb")
    except FileNotFoundError:
        return [], 0, False
    with source:
        for record in _records(source):
            kind = record.get("type")
            if kind == "log_rotated":
                total = record.get("offset", 0)
                truncated = offset < total
            elif kind in ("stdout", "stderr", "output"):
                if total >= offset:
                    selected.append({"type": kind, "data": record["data"]})
                    available += 1
                total += 1
    return list(selected), total, truncated or available > len(selected)


async def read_log(log_path: Optional[str], offset: int = 0, tail: Optional[int] = None) -> tuple[list[dict], int, bool]:
    """Read the requested output entries with the original absolute cursor semantics."""
    if not log_path:
        return [], 0, False
    return await run_io(_read_log, log_path, offset, tail)


def _log_response_end(next_offset: int, offset: int, truncated: bool) -> str:
    return '],"next_offset":' + str(next_offset) + ',"offset":' + str(offset) + ',"truncated":' + json.dumps(truncated) + '}'


_LOG_END_SIZE = len(_log_response_end(9_999_999_999, 9_999_999_999, False))


def _entry_bytes(entry: dict) -> int:
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1


def _build_log_response(path, metadata, offset, tail):
    """Serialize output to disk without retaining a list of all entries, up to the tool output budget."""
    from open_terminal.utils.fs import TemporaryFileResponse

    fd, temporary = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            head = json.dumps(metadata, ensure_ascii=False)[:-1] + ',"output":['
            output.write(head)
            budget = MAX_TOOL_OUTPUT_SIZE - len(head.encode("utf-8")) - _LOG_END_SIZE
            tail_sizes = deque()
            tail_bytes = 0
            total = available = 0
            first_index = stop_index = None
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
                            if tail is not None and total >= offset:
                                tail_sizes.append(_entry_bytes({"type": kind, "data": record["data"]}))
                                tail_bytes += tail_sizes[-1]
                                # One entry is always kept, so a cursor always advances.
                                while len(tail_sizes) > tail or (tail_bytes > budget and len(tail_sizes) > 1):
                                    tail_bytes -= tail_sizes.popleft()
                            total += 1
                    skip = available - len(tail_sizes) if tail is not None else 0
                    truncated |= skip > 0
                    source.seek(0)
                    index = 0
                    used = 0
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
                                    entry = {"type": kind, "data": record["data"]}
                                    used += _entry_bytes(entry)
                                    if tail is None and first_index is not None and used > budget:
                                        stop_index = index
                                        break
                                    if first_index is None:
                                        first_index = index
                                    output.write(separator)
                                    json.dump(entry, output, ensure_ascii=False)
                                    separator = ","
                            index += 1
            next_offset = total if stop_index is None else stop_index
            first_offset = next_offset if first_index is None else first_index
            output.write(_log_response_end(next_offset, first_offset, truncated or stop_index is not None))
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

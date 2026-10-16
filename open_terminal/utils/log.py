"""Process log management utilities.

Handles writing, reading, and capping JSONL log files for background
processes.  Extracted from ``main.py`` to keep the route module focused.
"""

import asyncio
import json
import os
import time
from collections import deque
from typing import Optional

import aiofiles
import aiofiles.os

from open_terminal.env import MAX_PROCESS_LOG_SIZE, LOG_FLUSH_INTERVAL, LOG_FLUSH_BUFFER, MAX_TOOL_OUTPUT_SIZE
from open_terminal.utils.limits import json_bytes


class BoundedLogWriter:
    """Async wrapper that rotates the log file when it exceeds a size limit.

    When the total bytes written surpass *MAX_PROCESS_LOG_SIZE*, the file
    is truncated to its newest half and a ``log_rotated`` marker is inserted.
    Writing then continues, so the most recent output is always available.

    Flushing behaviour is controlled by *flush_interval* and *flush_buffer*:

    * ``flush_interval=0`` (default) — flush after every write (original
      behaviour, safest for low-throughput commands).
    * ``flush_interval>0`` — flush at most once per *flush_interval* seconds,
      **or** when the unflushed buffer exceeds *flush_buffer* bytes (if set).
      This dramatically reduces I/O pressure for high-output commands.
    """

    __slots__ = (
        "_file", "_log_path", "_bytes_written", "rotated",
        "_flush_interval", "_flush_buffer", "_unflushed", "_last_flush",
        "_lock",
    )

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

    async def write(self, data: str) -> None:
        async with self._lock:
            await self._write(data)

    async def _write(self, data: str) -> None:
        encoded_len = len(data.encode("utf-8", errors="replace"))
        if self._bytes_written + encoded_len > MAX_PROCESS_LOG_SIZE:
            await self._rotate()
        await self._file.write(data)
        self._bytes_written += encoded_len
        self._unflushed += encoded_len

        if self._flush_interval <= 0:
            # Legacy behaviour: flush on every write.
            await self._file.flush()
            self._unflushed = 0
            return

        now = time.monotonic()
        should_flush = (now - self._last_flush) >= self._flush_interval
        if not should_flush and self._flush_buffer > 0:
            should_flush = self._unflushed >= self._flush_buffer
        if should_flush:
            await self._file.flush()
            self._unflushed = 0
            self._last_flush = now

    async def flush(self) -> None:
        await self._file.flush()
        self._unflushed = 0
        self._last_flush = time.monotonic()

    async def _rotate(self) -> None:
        """Keep the newest half of the log file and continue writing."""
        self.rotated = True
        await self._file.flush()
        # Close, rewrite, and reopen.
        await self._file.close()

        async with aiofiles.open(self._log_path, "r", encoding="utf-8") as f:
            lines = await f.readlines()

        # Keep the newest half of output lines.
        split = len(lines) // 2
        keep = lines[split:]
        offset = 0
        for line in lines[:split]:
            record = json.loads(line)
            if record.get("type") == "log_rotated":
                offset = record.get("offset", 0)
            elif record.get("type") in ("stdout", "stderr", "output"):
                offset += 1

        # Replace atomically so readers see either complete generation.
        temporary_path = self._log_path + ".rotating"
        async with aiofiles.open(temporary_path, "w", encoding="utf-8") as f:
            await f.write(
                json.dumps({"type": "log_rotated", "offset": offset, "ts": time.time()}) + "\n"
            )
            for line in keep:
                await f.write(line)
        await aiofiles.os.replace(temporary_path, self._log_path)

        # Reopen in append mode and reset byte counter.
        self._file = await aiofiles.open(self._log_path, "a", encoding="utf-8")
        self._bytes_written = sum(len(l.encode("utf-8", errors="replace")) for l in keep)




async def log_process(background_process) -> None:
    """Read process output and persist to a log file.

    When the file exceeds *MAX_PROCESS_LOG_SIZE*, the oldest half is
    discarded so the most recent output is always available.
    """
    log_file = None
    log_rotated = False
    try:
        if background_process.log_path:
            await aiofiles.os.makedirs(
                os.path.dirname(background_process.log_path), exist_ok=True
            )
            log_file = await aiofiles.open(background_process.log_path, "a", encoding="utf-8")
            await log_file.write(
                json.dumps(
                    {
                        "type": "start",
                        "command": background_process.command,
                        "pid": background_process.runner.pid,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
            await log_file.flush()
    except OSError:
        log_file = None

    # Wrap the log file so it rotates when the size limit is reached.
    writer = (
        BoundedLogWriter(
            log_file,
            background_process.log_path,
            flush_interval=LOG_FLUSH_INTERVAL,
            flush_buffer=LOG_FLUSH_BUFFER,
        )
        if log_file
        else None
    )

    try:
        await background_process.runner.read_output(writer)
    finally:
        log_rotated = writer.rotated if writer else False
        exit_code = await background_process.runner.wait()
        background_process.exit_code = exit_code
        background_process.runner.close()
        if writer:
            await writer.write(
                json.dumps(
                    {
                        "type": "end",
                        "exit_code": background_process.exit_code,
                        "log_rotated": log_rotated,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
            await writer.flush()
            await writer._file.close()
        if background_process.status != "killed":
            background_process.status = "done"
        background_process.finished_at = time.time()


async def read_log(
    log_path: Optional[str],
    offset: int = 0,
    tail: Optional[int] = None,
) -> tuple[list[dict], int, bool]:
    """Read output entries from a JSONL log file.

    Returns ``(entries, next_offset, truncated)``.

    Offsets count output entries across rotations. Tail reads retain only
    the requested entries in memory while still returning an absolute cursor.
    A sequential read stops at the entry that would exceed
    *MAX_TOOL_OUTPUT_SIZE* and reports it as the next cursor position.
    """
    entries: list[dict] = []
    if not log_path or not await aiofiles.os.path.isfile(log_path):
        return entries, 0, False

    selected = deque(maxlen=tail)
    used = 0
    total = 0
    available = 0
    truncated = False
    async with aiofiles.open(log_path, encoding="utf-8") as f:
        async for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "log_rotated":
                total = record.get("offset", 0)
                truncated = offset < total
            elif record.get("type") in ("stdout", "stderr", "output"):
                if total >= offset:
                    entry = {"type": record["type"], "data": record["data"]}
                    if tail is None:
                        used += json_bytes(entry)
                        if selected and used > MAX_TOOL_OUTPUT_SIZE:
                            return list(selected), total, True
                    selected.append(entry)
                    available += 1
                total += 1

    return list(selected), total, truncated or available > len(selected)

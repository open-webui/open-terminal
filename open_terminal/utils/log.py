"""Process log management utilities.

Handles writing, reading, and capping JSONL log files for background
processes.  Extracted from ``main.py`` to keep the route module focused.
"""

import json
import os
import time
from typing import Optional

import aiofiles
import aiofiles.os

from open_terminal.env import MAX_PROCESS_LOG_SIZE, LOG_FLUSH_INTERVAL, LOG_FLUSH_BUFFER

_last_disk_sweep = 0.0
_DISK_SWEEP_INTERVAL = 300  # rate-limit: at most once per 5 minutes


def sweep_expired_log_files(
    processes_dir: str, retention: float, now: Optional[float] = None
) -> list[str]:
    """Delete ``*.jsonl`` files in *processes_dir* older than *retention* seconds.

    Complements the in-memory-record-based cleanup in ``main.py``'s
    ``_cleanup_expired()``: that path only reaches a log file while its
    ``BackgroundProcess`` record is still in memory, but that registry
    resets on every process restart while the log files themselves live
    on a persistent volume. A log file older than the last restart is
    therefore permanently unreachable by that path alone, regardless of
    age. This reads mtimes directly off disk instead.

    Returns the list of paths actually deleted (for observability/tests).
    """
    now = time.time() if now is None else now
    deleted: list[str] = []
    try:
        entries = os.listdir(processes_dir)
    except OSError:
        return deleted
    for name in entries:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(processes_dir, name)
        try:
            if now - os.path.getmtime(path) > retention:
                os.remove(path)
                deleted.append(path)
        except OSError:
            pass
    return deleted


def sweep_expired_log_files_rate_limited(processes_dir: str, retention: float) -> list[str]:
    """Rate-limited wrapper around :func:`sweep_expired_log_files`.

    ``_cleanup_expired()`` runs on every process-status-check request,
    which can be frequent under active polling -- an unconditional
    directory listing on every call would add avoidable I/O. This limits
    the actual disk scan to at most once per *_DISK_SWEEP_INTERVAL*
    regardless of call frequency.
    """
    global _last_disk_sweep
    now = time.time()
    if now - _last_disk_sweep < _DISK_SWEEP_INTERVAL:
        return []
    _last_disk_sweep = now
    return sweep_expired_log_files(processes_dir, retention, now=now)


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

    async def write(self, data: str) -> None:
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
        keep = lines[len(lines) // 2 :]

        async with aiofiles.open(self._log_path, "w", encoding="utf-8") as f:
            await f.write(
                json.dumps({"type": "log_rotated", "ts": time.time()}) + "\n"
            )
            for line in keep:
                await f.write(line)

        # Reopen in append mode and reset byte counter.
        self._file = await aiofiles.open(self._log_path, "a", encoding="utf-8")
        self._bytes_written = sum(len(l.encode("utf-8", errors="replace")) for l in keep)


async def tail_log(log_path: str, n: int) -> list[dict]:
    """Read the last *n* output entries from a JSONL log without loading the whole file.

    Uses a reverse-read strategy: read chunks from the end of the file
    until enough newline-delimited records have been collected.
    """
    CHUNK = 8192
    entries: list[dict] = []

    async with aiofiles.open(log_path, "rb") as f:
        await f.seek(0, 2)  # seek to end
        remaining = await f.tell()
        buffer = b""

        while remaining > 0 and len(entries) < n:
            read_size = min(CHUNK, remaining)
            remaining -= read_size
            await f.seek(remaining)
            chunk = await f.read(read_size)
            buffer = chunk + buffer
            lines = buffer.split(b"\n")
            # The first element may be a partial line — keep it for next iteration.
            buffer = lines[0]
            for raw_line in reversed(lines[1:]):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if record.get("type") in ("stdout", "stderr", "output"):
                    entries.append({"type": record["type"], "data": record["data"]})
                    if len(entries) >= n:
                        break

        # Process any remaining buffer content.
        if buffer.strip() and len(entries) < n:
            try:
                record = json.loads(buffer)
                if record.get("type") in ("stdout", "stderr", "output"):
                    entries.append({"type": record["type"], "data": record["data"]})
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    entries.reverse()  # restore chronological order
    return entries[-n:]


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
        background_process.status = "done"
        background_process.finished_at = time.time()
        background_process.runner.close()
        if writer:
            # Flush any buffered output before writing the end marker.
            await writer.flush()
        if log_file:
            await log_file.write(
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
            await log_file.close()


async def read_log(
    log_path: Optional[str],
    offset: int = 0,
    tail: Optional[int] = None,
) -> tuple[list[dict], int, bool]:
    """Read output entries from a JSONL log file.

    Returns ``(entries, next_offset, truncated)``.

    When *tail* is specified and *offset* is 0, the file is read from the
    end to avoid loading the entire file into memory — preventing the
    memory spike that caused the OOM issue.
    """
    entries: list[dict] = []
    if not log_path or not await aiofiles.os.path.isfile(log_path):
        return entries, 0, False

    # --- Optimised tail-from-end path ---
    if tail is not None and offset == 0:
        tail_entries = await tail_log(log_path, tail)
        truncated = len(tail_entries) == tail  # may have been more
        return tail_entries, len(tail_entries), truncated

    # --- Full scan path (bounded by offset) ---
    async with aiofiles.open(log_path, encoding="utf-8") as f:
        lines = await f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") in ("stdout", "stderr", "output"):
            entries.append({"type": record["type"], "data": record["data"]})

    total = len(entries)
    entries = entries[offset:]

    truncated = False
    if tail is not None and len(entries) > tail:
        entries = entries[-tail:]
        truncated = True

    return entries, total, truncated

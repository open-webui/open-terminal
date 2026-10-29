"""Streaming I/O helpers with cancellation-safe resource cleanup."""

import asyncio
import json
import os

from fastapi import HTTPException

from open_terminal.env import MAX_TOOL_OUTPUT_SIZE

CHUNK_SIZE = 64 * 1024


async def run_io(function, *args):
    """Keep admission/cleanup ownership until a cancelled thread actually stops."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


class NormalizeNullQueryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from urllib.parse import parse_qsl, urlencode
            params = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            cleaned = [(key, value) for key, value in params if value.lower() != "null"]
            if len(cleaned) != len(params):
                scope = {**scope, "query_string": urlencode(cleaned).encode()}
        await self.app(scope, receive, send)


async def run_worker(command, payload=None, *, request=None, timeout=60, **options):
    """Run document work with cancellation and process-group cleanup."""
    import signal
    import subprocess

    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt", **options,
    )

    async def exchange():
        if payload:
            process.stdin.write(payload)
            await process.stdin.drain()
        process.stdin.close()
        output = bytearray()
        while chunk := await process.stdout.read(CHUNK_SIZE):
            output.extend(chunk)
        await process.wait()
        if process.returncode:
            raise HTTPException(422, "Document worker failed. Check that the document libraries are installed and the files are readable.")
        return bytes(output)

    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    task = asyncio.create_task(exchange())
    disconnect = asyncio.create_task(disconnected()) if request else None
    try:
        done, _ = await asyncio.wait(
            {task, disconnect} if disconnect else {task},
            timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise HTTPException(408, "Document operation exceeded its execution deadline.")
        if disconnect and disconnect in done:
            raise HTTPException(499, "Document operation cancelled.")
        return await task
    finally:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        task.cancel()
        if disconnect:
            disconnect.cancel()
        await asyncio.gather(*([task, disconnect] if disconnect else [task]), return_exceptions=True)
        process.stdin.close()
        # A paused stdout transport can keep Process.wait() pending even
        # after SIGKILL. Discard remaining bounded pipe chunks before reaping.
        while await process.stdout.read(CHUNK_SIZE):
            pass
        await process.wait()


def json_bytes(payload: object) -> int:
    """Size of *payload* serialized the way the API returns it."""
    return len(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


def escaped_bytes(text: str) -> int:
    """Size of *text* once JSON-escaped, as it lands in a response body."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def truncate_escaped(text: str, max_bytes: int) -> str:
    """Cut *text* so that its JSON-escaped size fits *max_bytes*."""
    low, high = 0, min(len(text), max_bytes)
    while low < high:
        middle = (low + high + 1) // 2
        if escaped_bytes(text[:middle]) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def excerpt_around(text: str, index: int, max_bytes: int) -> tuple[str, bool]:
    """Bounded window of *text* around the match at character *index*."""
    if len(text) <= max_bytes and len(text.encode("utf-8")) <= max_bytes:
        return text, False
    window_start = max(0, index - max_bytes)
    window = text[window_start : index + max_bytes].encode("utf-8")
    match_start = len(text[window_start:index].encode("utf-8"))
    start = min(max(0, match_start - max_bytes // 4), len(window) - max_bytes)
    return window[start : start + max_bytes].decode("utf-8", errors="ignore"), True


def fit_items(payload: dict, key: str) -> int:
    """Drop items from the end of ``payload[key]`` until *payload* fits the limit; returns how many."""
    items = payload[key]
    payload[key] = []
    used = json_bytes(payload)
    kept = []
    for item in items:
        used += json_bytes(item) + (1 if kept else 0)
        if used > MAX_TOOL_OUTPUT_SIZE and kept:
            break
        kept.append(item)
    payload[key] = kept
    return len(items) - len(kept)

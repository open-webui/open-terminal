"""Discover native models as the terminal user and cache the catalog."""

import asyncio
import json
import time

from fastapi import HTTPException, Request

from open_terminal.dependencies import get_filesystem
from . import sessions

catalogs: dict[tuple, tuple[float, dict]] = {}
refreshes: dict[tuple, asyncio.Task] = {}


async def probe(provider, fs, cwd=None):
    """Probe the installed CLI's native protocol as the terminal's OS user."""
    process, cwd = await sessions.spawn(
        provider, fs, sessions.Start(request_id="discovery", cwd=cwd)
    )
    stderr = asyncio.create_task(process.stderr.read())
    try:

        async def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()

        async def response(key, value):
            while line := await process.stdout.readline():
                message = json.loads(line)
                if "method" in message and "id" in message:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Discovery does not support interactive requests",
                            },
                        }
                    )
                    continue
                if message.get(key) == value:
                    return message
            raise RuntimeError("Agent exited during discovery")

        async with asyncio.timeout(20):
            return await sessions.PROVIDERS[provider].discover(send, response, cwd)
    finally:
        await sessions.stop_process(process)
        stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)


async def discover(
    request: Request, cwd: str | None = None, provider: str | None = None
):
    if provider is not None and provider not in sessions.PROVIDERS:
        raise HTTPException(400, "Unknown agent provider")
    fs = get_filesystem(request)
    scope = (request.headers.get("x-user-id", "single"), fs.home, cwd)

    async def detect(provider, name):
        try:
            models = await probe(provider, fs, cwd)
            # Claude aliases can resolve to the same model (as in Computer's discovery).
            models = list({model["id"]: model for model in models}.values())
            return {"id": provider, "name": name, "models": models, "available": True}
        except Exception as exc:
            return {
                "id": provider,
                "name": name,
                "models": [],
                "available": False,
                "error": exc.detail
                if isinstance(exc, HTTPException)
                else "Agent discovery failed; check installation and login in this terminal",
            }

    async def refresh(key, provider, name):
        try:
            entry = await detect(provider, name)
            catalogs[key] = (time.monotonic(), entry)
            return entry
        finally:
            refreshes.pop(key, None)

    async def entry(provider, adapter):
        key = (*scope, provider)
        cached = catalogs.get(key)
        if not cached or time.monotonic() - cached[0] >= 30:
            if key not in refreshes:
                refreshes[key] = asyncio.create_task(
                    refresh(key, provider, adapter.NAME)
                )
            if not cached:
                return await asyncio.shield(refreshes[key])
        # A known model must not wait for a slow discovery/login probe on every turn.
        return cached[1]

    return {
        "protocol_version": 1,
        "agents": await asyncio.gather(
            *(
                entry(p, adapter)
                for p, adapter in sessions.PROVIDERS.items()
                if provider is None or p == provider
            )
        ),
    }


async def shutdown():
    pending = list(refreshes.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    refreshes.clear()
    catalogs.clear()

import asyncio
import os
import sys
import tempfile

import httpx
import pytest

# open_terminal reads these at import time.
os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")
os.environ.setdefault("OPEN_TERMINAL_LOG_DIR", tempfile.mkdtemp())

from open_terminal import main

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")

# A command that ignores SIGTERM, like a stuck pager or a shell with a trap.
IGNORES_SIGTERM = "trap '' TERM; echo ready; while :; do sleep 0.1; done"


async def _start(client):
    response = await client.post("/execute", params={"wait": 0.5}, json={"command": IGNORES_SIGTERM})
    assert response.json()["status"] == "running"
    return main._processes[response.json()["id"]]


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {os.environ['OPEN_TERMINAL_API_KEY']}"},
    )


def test_kill_escalates_when_sigterm_is_ignored(monkeypatch):
    monkeypatch.setattr(main, "KILL_GRACE_PERIOD", 0.5)

    async def run():
        async with _client() as client:
            process = await _start(client)
            response = await asyncio.wait_for(client.delete(f"/execute/{process.id}"), 10)
            assert response.json() == {"status": "killed"}
            assert process.log_task.done()
            assert process.runner._process.poll() is not None

    asyncio.run(run())


def test_force_kill_after_graceful_kill_still_kills(monkeypatch):
    monkeypatch.setattr(main, "KILL_GRACE_PERIOD", 30)

    async def run():
        async with _client() as client:
            process = await _start(client)
            graceful = asyncio.create_task(client.delete(f"/execute/{process.id}"))
            await asyncio.sleep(0.5)
            assert process.status == "killed" and not process.log_task.done()

            response = await asyncio.wait_for(
                client.delete(f"/execute/{process.id}", params={"force": "true"}), 10
            )
            assert response.json() == {"status": "killed"}
            assert process.runner._process.poll() is not None
            await asyncio.wait_for(graceful, 10)

    asyncio.run(run())

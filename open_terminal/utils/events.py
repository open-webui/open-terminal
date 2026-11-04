"""Filesystem and port subscriptions for the shared events endpoint."""

import asyncio
import os
import shutil
import threading

import anyio
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from watchfiles import awatch

from open_terminal.utils.git import GitRepository


async def watch_paths(fs, path, cwd):
    repo = GitRepository(fs, path, cwd)
    paths = {repo.root}
    recursive = shutil.which("git") is not None and await repo.detect()
    if recursive:
        paths = {repo.root}
        # A linked worktree keeps its index and shared refs outside its working tree.
        for flag in ("--absolute-git-dir", "--git-common-dir"):
            _, value, _ = await repo.run("rev-parse", flag)
            paths.add(repo.checked_path(os.path.join(repo.root, value.removesuffix("\n"))))
    # Outside a repository only a new .git matters, and a recursive watch of a tree such as /
    # walks all of it on the event loop.
    return sorted(paths), recursive


async def system_events(ws: WebSocket, fs, cwd, ports):
    task = None
    port_task = None
    stop = threading.Event()

    async def stop_watcher():
        if task:
            stop.set()
            # ASGI servers can cancel the handler while its native watcher is exiting.
            with anyio.CancelScope(shield=True):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def send_ports():
        try:
            async for snapshot in ports():
                await ws.send_json({"type": "ports:change", "ports": snapshot})
        except Exception:
            await ws.close(code=1011, reason="Port discovery unavailable")

    async def send_changes(paths, recursive):
        ready = False
        try:
            # The first yield happens after the native watcher is installed. Refreshing
            # then closes the race between the initial status request and subscribing.
            async for changes in awatch(
                *paths, watch_filter=None, debounce=250, step=50,
                force_polling=False, yield_on_timeout=True, rust_timeout=1000,
                stop_event=stop, recursive=recursive,
            ):
                if not ready:
                    ready = True
                    await ws.send_json({"type": "fs:ready"})
                elif changes:
                    # No filenames are exposed; clients only need to invalidate status.
                    await ws.send_json({"type": "fs:change"})
        except (OSError, RuntimeError):
            await ws.close(code=1011, reason="Filesystem watcher unavailable")

    try:
        while True:
            payload = await asyncio.wait_for(ws.receive_json(), timeout=10 if task is None and port_task is None else None)
            if not isinstance(payload, dict) or payload.get("type") != "subscribe":
                await ws.close(code=4000, reason="Expected subscribe message")
                return
            path = payload.get("path")
            include_ports = payload.get("ports", False)
            if (type(include_ports) is not bool or
                path is not None and (not isinstance(path, str) or not path or len(path) > 4096)):
                await ws.close(code=4000, reason="Invalid subscription")
                return
            watch = await watch_paths(fs, path, cwd) if path is not None else None
            await stop_watcher()
            task = None
            stop = threading.Event()
            if watch is not None:
                task = asyncio.create_task(send_changes(*watch))
            if port_task:
                with anyio.CancelScope(shield=True):
                    port_task.cancel()
                    await asyncio.gather(port_task, return_exceptions=True)
            port_task = asyncio.create_task(send_ports()) if include_ports else None

    except WebSocketDisconnect:
        pass
    except (HTTPException, PermissionError):
        await ws.close(code=4003, reason="Watch path unavailable")
    except (TimeoutError, ValueError, KeyError):
        await ws.close(code=4000, reason="Invalid watch request")
    finally:
        await stop_watcher()
        if port_task:
            with anyio.CancelScope(shield=True):
                port_task.cancel()
                await asyncio.gather(port_task, return_exceptions=True)

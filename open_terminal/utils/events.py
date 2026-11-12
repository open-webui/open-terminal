"""Filesystem and port subscriptions for the shared events endpoint."""

import asyncio
import os
import shutil
import threading

import anyio
import watchfiles
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

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


_watchers: dict[tuple[tuple[str, ...], bool], "SharedWatcher"] = {}


class SharedWatcher:
    """Watches one set of paths for every client subscribed to them."""

    def __init__(self, key: tuple[tuple[str, ...], bool]):
        self.key: tuple[tuple[str, ...], bool] = key
        self.queues: set[asyncio.Queue] = set()
        self.ready: bool = False
        self.stop: threading.Event = threading.Event()
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        threading.Thread(target=self.run, name="fs-watcher", daemon=True).start()

    def run(self) -> None:
        paths, recursive = self.key
        try:
            # The first yield happens after the native watcher is installed. Refreshing
            # then closes the race between the initial status request and subscribing.
            for changes in watchfiles.watch(
                *paths, watch_filter=None, debounce=250, step=50,
                force_polling=False, yield_on_timeout=True, rust_timeout=1000,
                stop_event=self.stop, recursive=recursive,
            ):
                self.loop.call_soon_threadsafe(self.publish, changes)
        except (OSError, RuntimeError):
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.fail)

    def publish(self, changes: set[tuple[watchfiles.Change, str]]) -> None:
        if not self.ready:
            self.ready = True
            message = {"type": "fs:ready"}
        elif changes:
            # No filenames are exposed; clients only need to invalidate status.
            message = {"type": "fs:change"}
        else:
            return
        for queue in self.queues:
            # Any pending message already makes the client refresh.
            if not queue.full():
                queue.put_nowait(message)

    def fail(self) -> None:
        self.deregister()
        for queue in self.queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=1)
        if self.ready:
            queue.put_nowait({"type": "fs:ready"})
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.queues.discard(queue)
        if not self.queues:
            self.stop.set()
            self.deregister()

    def deregister(self) -> None:
        if _watchers.get(self.key) is self:
            del _watchers[self.key]


async def system_events(ws: WebSocket, fs, cwd, ports):
    task = None
    port_task = None

    async def stop_watcher():
        if task:
            # ASGI servers can cancel the handler before its cleanup has finished.
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
        key = (tuple(paths), recursive)
        if key not in _watchers:
            _watchers[key] = SharedWatcher(key)
        watcher = _watchers[key]
        queue = watcher.subscribe()
        try:
            while (message := await queue.get()) is not None:
                await ws.send_json(message)
            await ws.close(code=1011, reason="Filesystem watcher unavailable")
        finally:
            watcher.unsubscribe(queue)

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

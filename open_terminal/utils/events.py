"""Filesystem and port subscriptions for the shared events endpoint."""

import asyncio
import os
import shutil
import threading

import anyio
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
import watchfiles

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


class _FSWatcher:
    """One native watcher shared by every client watching the same paths.

    It runs on its own thread rather than the worker pool that file requests queue for, and
    clients on the same folder count once against the host's watcher limit.
    """

    def __init__(self, key: tuple[tuple[str, ...], bool]):
        self.key = key
        self.subscribers: set[asyncio.Queue] = set()
        self.ready = False
        self.stop = threading.Event()
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._watch, name="fs-watcher", daemon=True).start()

    def _watch(self):
        paths, recursive = self.key
        try:
            # The first yield happens after the native watcher is installed. Refreshing
            # then closes the race between the initial status request and subscribing.
            for changes in watchfiles.watch(
                *paths, watch_filter=None, debounce=250, step=50,
                force_polling=False, yield_on_timeout=True, rust_timeout=1000,
                stop_event=self.stop, recursive=recursive,
            ):
                self.loop.call_soon_threadsafe(self._notify, changes)
        except (OSError, RuntimeError) as exc:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self._fail, exc)

    def _notify(self, changes):
        if not self.ready:
            self.ready = True
            event = "fs:ready"
        elif changes:
            # No filenames are exposed; clients only need to invalidate status.
            event = "fs:change"
        else:
            return
        for subscriber in self.subscribers:
            # A pending event already makes the client refresh.
            if not subscriber.full():
                subscriber.put_nowait(event)

    def _fail(self, exc):
        self._forget()
        for subscriber in self.subscribers:
            if subscriber.full():
                subscriber.get_nowait()
            subscriber.put_nowait(exc)

    def _forget(self):
        if _fs_watchers.get(self.key) is self:
            del _fs_watchers[self.key]

    def close(self):
        self.stop.set()
        self._forget()


_fs_watchers: dict[tuple[tuple[str, ...], bool], _FSWatcher] = {}


async def watch_filesystem(paths, recursive):
    """Yield "fs:ready" once the watcher is installed, then "fs:change" after each change."""
    key = (tuple(paths), recursive)
    watcher = _fs_watchers.get(key)
    if watcher is None:
        watcher = _fs_watchers[key] = _FSWatcher(key)
    queue = asyncio.Queue(maxsize=1)
    if watcher.ready:
        queue.put_nowait("fs:ready")
    watcher.subscribers.add(queue)
    try:
        while True:
            event = await queue.get()
            if isinstance(event, Exception):
                raise event
            yield event
    finally:
        watcher.subscribers.discard(queue)
        if not watcher.subscribers:
            watcher.close()


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
        try:
            async for event in watch_filesystem(paths, recursive):
                await ws.send_json({"type": event})
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

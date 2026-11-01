"""Headless Chrome tabs. CDP stays on private pipes, never a shared TCP port."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys

from fastapi import WebSocket, WebSocketDisconnect
from open_terminal.env import sanitized_environ
from .rewrite import target_url

log = logging.getLogger(__name__)


def find_browser():
    configured = os.environ.get("OPEN_TERMINAL_BROWSER_EXECUTABLE")
    candidates = [configured] if configured else []
    candidates += [
        shutil.which(name)
        for name in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "msedge",
        )
    ]
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def lock_profile(profile):
    import fcntl
    import socket

    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    lease = os.open(profile / ".open-terminal.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        managed = os.read(lease, 8) == b"managed\n"
        singleton = profile / "SingletonLock"
        if singleton.is_symlink():
            hostname, pid = os.readlink(singleton).rsplit("-", 1)
            if hostname == socket.gethostname():
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError("Chrome profile is still in use")
            elif not managed:
                # Older launches have no lease, so another host may still own this profile.
                raise RuntimeError("Chrome profile is locked by another host")
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = profile / name
            if path.is_symlink():
                path.unlink()
        os.lseek(lease, 0, os.SEEK_SET)
        os.write(lease, b"managed\n")
        # Chrome keeps this lease across exec; the kernel releases it even after a crash.
        os.set_inheritable(lease, True)
        return lease
    except BaseException:
        os.close(lease)
        raise


class Host:
    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.pending = {}
        self.handlers = {}
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            while True:
                raw = await self.process.stdout.readuntil(b"\0")
                message = json.loads(raw[:-1])
                if "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(message["error"]["message"])
                            )
                        else:
                            future.set_result(message.get("result", {}))
                elif handler := self.handlers.get(message.get("sessionId")):
                    # Handlers never wait on CDP, so its reader remains available for responses.
                    handler(message.get("method"), message.get("params", {}))
        except (Exception, asyncio.CancelledError) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                log.debug("Chrome pipe closed: %s", type(exc).__name__)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Chrome disconnected"))
            self.pending.clear()

    async def send(self, method, params=None, session=None):
        if self.reader.done():
            raise RuntimeError("Chrome disconnected. Reopen this browser tab.")
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        message = {"id": number, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            self.process.stdin.write(json.dumps(message).encode() + b"\0")
            await self.process.stdin.drain()
            try:
                return await asyncio.wait_for(future, 15)
            except TimeoutError as exc:
                raise RuntimeError(f"Chrome did not respond to {method}") from exc
        finally:
            self.pending.pop(number, None)

    async def close(self):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.send("Browser.close"), 2)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


hosts = {}
host_lock = asyncio.Lock()


async def get_host(session):
    async with host_lock:
        if session.owner in hosts and hosts[session.owner].process.returncode is None:
            return hosts[session.owner]
        binary = find_browser()
        if not binary:
            raise RuntimeError(
                "Chrome mode requires Chromium. Install it or set OPEN_TERMINAL_BROWSER_EXECUTABLE."
            )
        if os.name == "nt":
            raise RuntimeError("Managed Chrome currently requires a POSIX host")
        # Let Chrome create its own profile as the same OS user as the terminal.
        suffix = hashlib.sha256(session.owner.encode()).hexdigest()[:24]
        profile = str(
            Path(session.fs.home) / ".local/state/open-terminal/browser" / suffix
        )
        args = [
            binary,
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ]
        if Path("/.dockerenv").exists():
            args.append("--no-sandbox")
        args.append("about:blank")
        # Chrome's pipe transport uses fd 3/4. A tiny exec shim maps subprocess stdin/stdout.
        shim = (
            "import os,sys; from open_terminal.browser.chrome import lock_profile; "
            "os.dup2(0,3); os.dup2(1,4); lease=lock_profile(sys.argv[1]); "
            "os.execv(sys.argv[2],sys.argv[2:])"
        )
        command = [sys.executable, "-c", shim, profile, *args]
        kwargs = {}
        if session.fs.username:
            import pwd

            account = pwd.getpwnam(session.fs.username)
            if os.getuid() == 0:
                kwargs.update(
                    user=account.pw_uid, group=account.pw_gid, extra_groups=[]
                )
            else:
                command = [
                    "sudo",
                    "-H",
                    "-n",
                    "-u",
                    session.fs.username,
                    "--",
                    *command,
                ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=sanitized_environ({"HOME": session.fs.home}),
            limit=16 * 1024 * 1024,
            **kwargs,
        )
        host = Host(process)
        try:
            await host.send("Browser.getVersion")
        except Exception:
            await host.close()
            raise RuntimeError(
                "Chrome could not start. Check its installation, OS user, and sandbox support."
            )
        hosts[session.owner] = host
        return host


async def shutdown_hosts():
    await asyncio.gather(
        *(host.close() for host in hosts.values()), return_exceptions=True
    )
    hosts.clear()


class Chrome:
    def __init__(self, session, host, target, cdp_session):
        self.session, self.host, self.target, self.cdp_session = (
            session,
            host,
            target,
            cdp_session,
        )
        self.peer = None
        self.frame = asyncio.Queue(maxsize=1)
        self.state_changed = asyncio.Event()
        self.width, self.height = 1024, 768
        self.quality = 65
        self.visible = True
        self.navigation = None
        self.error = ""
        host.handlers[cdp_session] = self.event

    @classmethod
    async def create(cls, session):
        host = await get_host(session)
        chrome = None
        target = (await host.send("Target.createTarget", {"url": "about:blank"}))[
            "targetId"
        ]
        try:
            attached = await host.send(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )
            chrome = cls(session, host, target, attached["sessionId"])
            await chrome.send("Page.enable")
            await chrome.send("Runtime.enable")
            await chrome.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": """
                window.open = url => { if (url) location.assign(url); return null; };
                addEventListener('click', e => {const a=e.target.closest?.('a[target]');
                  if(a && a.target !== '_self'){e.preventDefault(); location.assign(a.href);}},true);
            """},
            )
            if session.url:
                chrome.navigate(session.url)
            return chrome
        except BaseException:
            if chrome:
                host.handlers.pop(chrome.cdp_session, None)
            with contextlib.suppress(Exception):
                await host.send("Target.closeTarget", {"targetId": target})
            raise

    async def send(self, method, params=None):
        return await self.host.send(method, params, self.cdp_session)

    def navigate(self, url):
        url = target_url(url)
        if self.navigation:
            self.navigation.cancel()
        self.error = ""

        async def load():
            try:
                result = await self.send("Page.navigate", {"url": url})
                self.error = result.get("errorText", "")
            except RuntimeError as exc:
                self.error = str(exc)
            finally:
                self.state_changed.set()

        # Slow sites must not block opening the viewer or its navigation controls.
        self.navigation = asyncio.create_task(load())

    def event(self, method, params):
        if method == "Page.screencastFrame":
            # Chrome applies backpressure until the frame is acknowledged.
            if self.frame.full():
                self.frame.get_nowait()
            self.frame.put_nowait(params)
        elif method in (
            "Page.frameNavigated",
            "Page.navigatedWithinDocument",
            "Page.loadEventFired",
        ):
            if method == "Page.loadEventFired":
                self.error = ""
            self.state_changed.set()

    async def state(self):
        try:
            history = await self.send("Page.getNavigationHistory")
        except RuntimeError as exc:
            # Chrome briefly detaches the renderer during cross-site navigation.
            if str(exc) == "Not attached to an active page":
                return None
            raise
        entries, index = history["entries"], history["currentIndex"]
        entry = entries[index] if entries else {}
        url = entry.get("url", "")
        if url.startswith(("http://", "https://")):
            self.session.url = url
        self.session.title = entry.get("title", "")
        return {
            "type": "state",
            "url": self.session.url,
            "title": self.session.title,
            "error": self.error,
            "canGoBack": index > 0,
            "canGoForward": index < len(entries) - 1,
        }

    async def capture(self):
        await self.send("Page.stopScreencast")
        if not self.visible:
            return
        await self.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self.quality,
                "maxWidth": self.width,
                "maxHeight": self.height,
                "everyNthFrame": 1,
            },
        )

    async def stream(self, ws: WebSocket):
        await ws.accept()
        if self.peer:
            with contextlib.suppress(Exception):
                await self.peer.close(
                    code=4000, reason="Browser opened in another viewer"
                )
        self.peer = ws
        tasks = []
        try:
            if state := await self.state():
                await ws.send_json(state)
            await self.capture()

            async def frames():
                while True:
                    frame = await self.frame.get()
                    await ws.send_json(
                        {
                            "type": "frame",
                            "data": frame["data"],
                            "width": self.width,
                            "height": self.height,
                        }
                    )
                    await self.send(
                        "Page.screencastFrameAck", {"sessionId": frame["sessionId"]}
                    )

            async def states():
                while True:
                    await self.state_changed.wait()
                    self.state_changed.clear()
                    if state := await self.state():
                        await ws.send_json(state)

            async def commands():
                while True:
                    msg = await ws.receive_json()
                    if not isinstance(msg, dict):
                        continue
                    kind = msg.get("type")
                    if kind == "navigate":
                        self.navigate(str(msg.get("url", "")))
                    elif kind in ("back", "forward"):
                        history = await self.send("Page.getNavigationHistory")
                        index = history["currentIndex"] + (-1 if kind == "back" else 1)
                        if 0 <= index < len(history["entries"]):
                            await self.send(
                                "Page.navigateToHistoryEntry",
                                {"entryId": history["entries"][index]["id"]},
                            )
                    elif kind == "reload":
                        await self.send("Page.reload")
                    elif kind == "visibility":
                        self.visible = msg.get("visible") is True
                        await self.capture()
                    elif kind == "viewport":
                        self.width = max(240, min(2560, int(msg.get("width", 1024))))
                        self.height = max(200, min(2160, int(msg.get("height", 768))))
                        mobile = msg.get("mobile") is True
                        await self.send(
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": self.width,
                                "height": self.height,
                                "deviceScaleFactor": 1,
                                "mobile": mobile,
                            },
                        )
                        await self.send(
                            "Emulation.setTouchEmulationEnabled", {"enabled": mobile}
                        )
                        await self.send(
                            "Emulation.setUserAgentOverride",
                            {
                                "userAgent": (
                                    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
                                    if mobile
                                    else ""
                                )
                            },
                        )
                        await self.capture()
                    elif kind == "quality":
                        self.quality = {"low": 40, "balanced": 65, "crisp": 90}.get(
                            msg.get("quality"), 65
                        )
                        await self.capture()
                    elif kind == "mouse":
                        event = {
                            k: msg[k]
                            for k in (
                                "type",
                                "x",
                                "y",
                                "button",
                                "buttons",
                                "clickCount",
                                "deltaX",
                                "deltaY",
                                "modifiers",
                            )
                            if k in msg
                        }
                        event["type"] = msg.get("event")
                        if event["type"] in (
                            "mousePressed",
                            "mouseReleased",
                            "mouseMoved",
                            "mouseWheel",
                        ):
                            await self.send("Input.dispatchMouseEvent", event)
                    elif kind == "key":
                        event = {
                            k: msg[k]
                            for k in (
                                "key",
                                "code",
                                "text",
                                "modifiers",
                                "windowsVirtualKeyCode",
                                "commands",
                            )
                            if k in msg
                        }
                        event["type"] = msg.get("event")
                        if event["type"] in ("keyDown", "keyUp"):
                            await self.send("Input.dispatchKeyEvent", event)
                    elif kind == "paste":
                        await self.send(
                            "Input.insertText",
                            {"text": str(msg.get("text", ""))[:1_000_000]},
                        )

            tasks = [
                asyncio.create_task(frames()),
                asyncio.create_task(states()),
                asyncio.create_task(commands()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError, ValueError, TypeError):
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.peer is ws:
                self.peer = None
                with contextlib.suppress(Exception):
                    await self.send("Page.stopScreencast")

    async def close(self):
        if self.navigation:
            self.navigation.cancel()
            await asyncio.gather(self.navigation, return_exceptions=True)
        if self.peer:
            with contextlib.suppress(Exception):
                await self.peer.close()
        self.host.handlers.pop(self.cdp_session, None)
        with contextlib.suppress(Exception):
            await self.host.send("Target.closeTarget", {"targetId": self.target})

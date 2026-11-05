"""Managed Chrome tabs or attachments to an existing Chrome over CDP."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from itertools import count
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

import httpx
import websockets

from fastapi import WebSocket, WebSocketDisconnect
from open_terminal.env import (
    BROWSER_CDP_URL, BROWSER_MODE, BROWSER_NO_SANDBOX, BROWSER_USER_DATA_DIR,
    sanitized_environ,
)
from .rewrite import target_url

log = logging.getLogger(__name__)
ref_ids = count(1)


def find_browser():
    configured = os.environ.get("OPEN_TERMINAL_BROWSER_EXECUTABLE")
    candidates = [configured] if configured else []
    candidates += [
        shutil.which(name)
        for name in (
            "chromium",
            "chrome",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "msedge",
        )
    ]
    if os.name == "nt":
        for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if directory := os.environ.get(root):
                candidates += [
                    str(Path(directory) / path)
                    for path in (
                        "Google/Chrome/Application/chrome.exe",
                        "Microsoft/Edge/Application/msedge.exe",
                        "Chromium/Application/chrome.exe",
                    )
                ]
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


async def personal_endpoint():
    if BROWSER_CDP_URL:
        endpoint = BROWSER_CDP_URL.strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get(endpoint.rstrip("/") + "/json/version")
                response.raise_for_status()
                endpoint = response.json().get("webSocketDebuggerUrl", "")
            parsed = urlsplit(endpoint)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("Chrome CDP URL must be an HTTP address or a WebSocket endpoint")
        return endpoint

    if BROWSER_USER_DATA_DIR:
        directory = Path(BROWSER_USER_DATA_DIR).expanduser()
    elif sys.platform == "darwin":
        directory = Path.home() / "Library/Application Support/Google/Chrome"
    elif os.name == "nt":
        directory = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Google/Chrome/User Data"
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "google-chrome"
    path = directory / "DevToolsActivePort"
    try:
        lines = (await asyncio.to_thread(path.read_text, encoding="utf-8")).splitlines()
        port = int(lines[0])
        if not 0 < port < 65536 or not re.fullmatch(r"/devtools/browser(?:/[\w-]+)?", lines[1]):
            raise ValueError("Invalid debugging endpoint")
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Cannot discover Chrome from {path}. Start Chrome and enable debugging at "
            "chrome://inspect/#remote-debugging, or set OPEN_TERMINAL_BROWSER_CDP_URL."
        ) from exc
    return f"ws://127.0.0.1:{port}{lines[1]}"


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
    def __init__(self, process=None, display=None, channel=None):
        self.process = process
        self.channel = channel
        self.display = display
        self.viewer = None
        self.display_mode = None
        self.view_lock = asyncio.Lock()
        self.sequence = 0
        self.pending = {}
        self.handlers = {}
        self.stderr = b""
        self.stderr_reader = asyncio.create_task(self.read_stderr())
        self.reader = asyncio.create_task(self.read())

    async def read_stderr(self):
        if self.process is not None and self.process.stderr is not None:
            while chunk := await self.process.stderr.read(4096):
                self.stderr = (self.stderr + chunk)[-4096:]

    async def read(self):
        try:
            while True:
                raw = (
                    await self.channel.recv()
                    if self.channel
                    else (await self.process.stdout.readuntil(b"\0"))[:-1]
                )
                message = json.loads(raw)
                if "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(message["error"]["message"])
                            )
                        else:
                            future.set_result(message.get("result", {}))
                elif message.get("method") == "Target.detachedFromTarget":
                    if handler := self.handlers.get(message.get("params", {}).get("sessionId")):
                        handler("Inspector.detached", {"reason": "Chrome tab detached"})
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
            for handler in list(self.handlers.values()):
                handler("Inspector.detached", {"reason": "Chrome disconnected"})

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
            if self.channel:
                await self.channel.send(json.dumps(message))
            else:
                self.process.stdin.write(json.dumps(message).encode() + b"\0")
                await self.process.stdin.drain()
            try:
                return await asyncio.wait_for(future, 15)
            except TimeoutError as exc:
                raise RuntimeError(f"Chrome did not respond to {method}") from exc
        except (OSError, websockets.ConnectionClosed) as exc:
            raise RuntimeError("Chrome disconnected. Reopen this browser tab.") from exc
        finally:
            self.pending.pop(number, None)

    async def close(self):
        if self.process is not None:
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
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stderr_reader, 1)
        if self.channel:
            await self.channel.close()
        if self.display:
            await self.display.close()


hosts = {}
host_lock = asyncio.Lock()


async def windows_host(args, profile, home):
    # Chrome on Windows exposes CDP on a random loopback port, scoped to this profile.
    path = Path(profile) / "DevToolsActivePort"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    process = await asyncio.create_subprocess_exec(
        *args,
        "--headless=new",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=sanitized_environ({"HOME": home}),
    )
    try:
        for _ in range(150):
            if process.returncode is not None:
                raise RuntimeError(
                    "Chrome exited during startup. Close any other process using its profile."
                )
            if path.exists():
                lines = path.read_text().splitlines()
                if len(lines) == 2 and lines[1].startswith("/devtools/browser/"):
                    channel = await websockets.connect(
                        f"ws://127.0.0.1:{int(lines[0])}{lines[1]}",
                        max_size=16 * 1024 * 1024,
                    )
                    return Host(process, channel=channel)
            await asyncio.sleep(0.1)
        raise RuntimeError("Chrome did not open its debugging connection")
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


async def get_host(session):
    async with host_lock:
        if BROWSER_MODE == "personal" and session.fs.username:
            raise RuntimeError("Personal Chrome requires single-user mode")
        key = (session.owner, "personal" if BROWSER_MODE == "personal" else session.fs.home)
        if key in hosts and not hosts[key].reader.done() and (
            hosts[key].process is None or hosts[key].process.returncode is None
        ):
            return hosts[key]
        if key in hosts:
            await hosts.pop(key).close()
        if BROWSER_MODE == "personal":
            host = None
            try:
                channel = await websockets.connect(
                    await personal_endpoint(), max_size=16 * 1024 * 1024,
                    open_timeout=20, close_timeout=2,
                )
                host = Host(channel=channel)
                await host.send("Browser.getVersion")
            except BaseException as exc:
                if host:
                    await host.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeError(
                    f"Could not connect to personal Chrome: {exc}. "
                    "Enable debugging at chrome://inspect/#remote-debugging and approve the connection in Chrome."
                ) from exc
            hosts[key] = host
            return host
        binary = find_browser()
        if not binary:
            raise RuntimeError(
                "Chrome mode requires Chromium. Install it or set OPEN_TERMINAL_BROWSER_EXECUTABLE."
            )
        # Let Chrome create its own profile as the same OS user as the terminal.
        suffix = hashlib.sha256(session.owner.encode()).hexdigest()[:24]
        profile = str(
            Path(session.fs.home) / ".local/state/open-terminal/browser" / suffix
        )
        args = [
            binary,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ]
        if BROWSER_NO_SANDBOX:
            args.append("--no-sandbox")
        if os.name == "nt":
            host = await windows_host(args, profile, session.fs.home)
            hosts[key] = host
            return host
        args.append("--remote-debugging-pipe")
        display = None
        if all(
            shutil.which(name) for name in ("Xtigervnc", "xauth", "xrandr", "xdotool")
        ):
            from open_terminal.desktop import Desktop, startup_lock

            display = Desktop(session.fs)
            try:
                async with startup_lock:
                    await display.start(shell=False)
            except BaseException:
                await display.close()
                raise
            args += ["--kiosk", "--window-position=0,0", "--window-size=1280,800"]
        else:
            args.append("--headless=new")
        args.append("about:blank")
        # Chrome's pipe transport uses fd 3/4. A tiny exec shim maps subprocess stdin/stdout.
        shim = (
            "import os,sys; from open_terminal.browser.chrome import lock_profile; "
            "os.dup2(0,3); os.dup2(1,4); lease=lock_profile(sys.argv[1]); "
            "os.execv(sys.argv[2],sys.argv[2:])"
        )
        command = [sys.executable, "-c", shim, profile, *args]
        kwargs = {}
        if session.fs.username and not display:
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
        if not display:
            kwargs["env"] = sanitized_environ({"HOME": session.fs.home})
        try:
            process = await (
                display.spawn if display else asyncio.create_subprocess_exec
            )(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
                **kwargs,
            )
        except BaseException:
            if display:
                await display.close()
            raise
        host = Host(process, display)
        try:
            await host.send("Browser.getVersion")
        except Exception as exc:
            await host.close()
            detail = host.stderr.decode(errors="replace").strip() or str(exc)
            log.warning("Chrome startup failed: %s", detail)
            raise RuntimeError(f"Chrome could not start: {detail}") from exc
        hosts[key] = host
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
        self.vnc_peer = None
        self.acks = set()
        self.refs = {}
        self.detached = False
        self.closed = False
        host.handlers[cdp_session] = self.event

    @classmethod
    async def create(cls, session):
        host = await get_host(session)
        chrome = None
        target = (
            await host.send(
                "Target.createTarget",
                {"url": "about:blank", "background": bool(host.display)},
            )
        )["targetId"]
        try:
            attached = await host.send(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )
            chrome = cls(session, host, target, attached["sessionId"])
            await chrome.send("Page.enable")
            await chrome.send("Runtime.enable")
            if host.process is None:
                # Match viewer coordinates before its first viewport message, even on Retina displays.
                await chrome.send(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": chrome.width, "height": chrome.height, "deviceScaleFactor": 1, "mobile": False},
                )
            await chrome.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                window.open = url => { if (url) location.assign(url); return null; };
                addEventListener('click', e => {const a=e.target.closest?.('a[target]');
                  if(a && a.target !== '_self'){e.preventDefault(); location.assign(a.href);}},true);
            """
                },
            )
            if session.url:
                chrome.navigate(session.url)
            return chrome
        except BaseException:
            if chrome:
                await chrome.close()
            else:
                with contextlib.suppress(Exception):
                    await host.send("Target.closeTarget", {"targetId": target})
            raise

    async def send(self, method, params=None):
        if self.detached or self.closed:
            raise RuntimeError("Chrome tab disconnected. Reopen this browser tab.")
        return await self.host.send(method, params, self.cdp_session)

    async def snapshot(self):
        await self.state()
        result = await self.send("Accessibility.getFullAXTree")
        nodes = {node["nodeId"]: node for node in result.get("nodes", [])}
        previous = {node_id: ref for ref, node_id in self.refs.items()}
        refs, lines = {}, []
        interactive = {
            "link", "button", "textbox", "searchbox", "combobox", "checkbox",
            "radio", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
            "option", "switch", "slider", "spinbutton", "treeitem",
        }
        stack = [(node, 0, "") for node in reversed(list(nodes.values()))
                 if node.get("parentId") not in nodes]
        while stack:
            node, depth, parent_name = stack.pop()
            role = node.get("role", {}).get("value", "")
            name = node.get("name", {}).get("value", "")
            properties = {p["name"]: p.get("value", {}).get("value")
                          for p in node.get("properties", [])}
            show = not node.get("ignored") and role not in ("", "none", "generic", "InlineTextBox")
            if role == "StaticText" and name == parent_name:
                show = False
            if show:
                label = role
                backend_id = node.get("backendDOMNodeId")
                if backend_id and (role in interactive or properties.get("focusable")):
                    ref = previous.get(backend_id) or f"@e{next(ref_ids)}"
                    refs[ref] = backend_id
                    label += f" {ref}"
                if name:
                    label += " " + json.dumps(name, ensure_ascii=False)
                if "value" in node:
                    label += " value=" + json.dumps(node["value"].get("value"), ensure_ascii=False)
                for prop in ("checked", "pressed", "selected", "expanded", "disabled", "required", "readonly", "level"):
                    if prop in properties:
                        label += f" {prop}=" + json.dumps(properties[prop])
                lines.append("  " * depth + label)
            for child_id in reversed(node.get("childIds", [])):
                if child_id in nodes:
                    stack.append((nodes[child_id], depth + int(show), name if show else parent_name))
        self.refs = refs
        return {
            "session_id": self.session.id,
            "url": self.session.url,
            "title": self.session.title,
            "snapshot": "\n".join(lines) or "[empty page]",
        }

    def ref_node(self, ref):
        node_id = self.refs.get("@" + ref.lstrip("@"))
        if node_id is None:
            raise ValueError("Unknown or expired element ref. Take a new browser_snapshot.")
        return {"backendNodeId": node_id}

    async def ref_position(self, node):
        await self.send("DOM.scrollIntoViewIfNeeded", node)
        result = await self.send("DOM.getContentQuads", node)
        for quad in result.get("quads", []):
            if len(quad) == 8 and max(quad[::2]) > min(quad[::2]) and max(quad[1::2]) > min(quad[1::2]):
                return sum(quad[::2]) / 4, sum(quad[1::2]) / 4
        raise ValueError("Element has no visible bounds. Take a new browser_snapshot.")

    def navigate(self, url):
        url = target_url(url)
        self.refs.clear()
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
        if method == "Inspector.detached":
            self.detached = True
            self.refs.clear()
            self.state_changed.set()
        elif method == "Page.screencastFrame":
            # A slow viewer must not block Chrome; retain only its newest full frame.
            task = asyncio.create_task(
                self.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            )
            self.acks.add(task)

            def done(task):
                self.acks.discard(task)
                if not task.cancelled():
                    task.exception()

            task.add_done_callback(done)
            if self.frame.full():
                self.frame.get_nowait()
            self.frame.put_nowait(params)
        elif method in (
            "Page.frameNavigated",
            "Page.navigatedWithinDocument",
            "Page.loadEventFired",
            "Page.frameResized",
        ):
            if method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId"):
                self.refs.clear()
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
            "transport": "vnc" if self.host.display else "frames",
        }

    async def capture(self):
        if self.host.display:
            return
        await self.send("Page.stopScreencast")
        if not self.visible:
            return
        await self.send("Page.bringToFront")
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

    async def resize_display(self):
        display = self.host.display
        mode = f"browser-{self.width}x{self.height}"
        if self.host.display_mode != mode:
            w, h = str(self.width), str(self.height)
            await display.run("xrandr", "--newmode", mode, "0", w, w, w, w, h, h, h, h)
            await display.run("xrandr", "--addmode", "VNC-0", mode)
            await display.run("xrandr", "--output", "VNC-0", "--mode", mode)
            previous, self.host.display_mode = self.host.display_mode, mode
            if previous:
                await display.run("xrandr", "--delmode", "VNC-0", previous)
                await display.run("xrandr", "--rmmode", previous)
        await self.send("Page.bringToFront")
        # X11 sizing avoids Chrome's minimum window width in mobile preview.
        await display.run(
            "xdotool",
            "getwindowfocus",
            "windowsize",
            str(self.width),
            str(self.height),
            "windowmove",
            "0",
            "0",
        )

    async def stream_vnc(self, ws):
        await ws.accept()
        try:
            async with self.host.view_lock:
                # A workspace browser has one visible page; keep its other tabs running.
                for peer in list(self.host.display.peers):
                    await peer.close(code=4000)
                self.host.viewer = self
                self.vnc_peer = ws
                await self.resize_display()
                self.host.display.peers.add(ws)
            await self.host.display.stream(ws)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            self.host.display.peers.discard(ws)
            if self.vnc_peer is ws:
                self.vnc_peer = None
                if self.host.viewer is self:
                    self.host.viewer = None
            with contextlib.suppress(Exception):
                await ws.close()

    async def stream(self, ws: WebSocket):
        await ws.accept()
        if self.peer:
            with contextlib.suppress(Exception):
                await self.peer.close(
                    code=4000, reason="Browser opened in another viewer"
                )
        self.peer = ws
        tasks = []
        binary_frames = False
        ready = asyncio.Event()
        ready.set()
        pending_mouse = {}
        mouse_changed = asyncio.Event()
        try:
            if state := await self.state():
                await ws.send_json(state)
            await self.capture()

            async def frames():
                from open_terminal.frames import Frames

                encoder = Frames()
                while True:
                    await ready.wait()
                    frame = await self.frame.get()
                    if binary_frames:
                        packet = await asyncio.to_thread(
                            encoder.jpeg, base64.b64decode(frame["data"]), self.quality
                        )
                        if packet is not None:
                            ready.clear()
                            await ws.send_bytes(packet)
                    else:
                        await ws.send_json(
                            {
                                "type": "frame",
                                "data": frame["data"],
                                "width": self.width,
                                "height": self.height,
                            }
                        )

            async def mouse_commands():
                while True:
                    await mouse_changed.wait()
                    events = list(pending_mouse.values())
                    pending_mouse.clear()
                    mouse_changed.clear()
                    for event in events:
                        await self.send("Input.dispatchMouseEvent", event)

            async def states():
                while True:
                    await self.state_changed.wait()
                    self.state_changed.clear()
                    if state := await self.state():
                        await ws.send_json(state)

            async def commands():
                nonlocal binary_frames
                while True:
                    msg = await ws.receive_json()
                    if not isinstance(msg, dict):
                        continue
                    kind = msg.get("type")
                    if kind == "frames":
                        binary_frames = True
                        await self.capture()
                    elif kind == "frameAck":
                        ready.set()
                    elif kind == "navigate":
                        pending_mouse.clear()
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
                        if self.host.display and self.host.viewer is self:
                            async with self.host.view_lock:
                                if self.host.viewer is self:
                                    await self.resize_display()
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
                            if event["type"] in ("mouseMoved", "mouseWheel"):
                                if event["type"] == "mouseWheel" and (
                                    previous := pending_mouse.get("mouseWheel")
                                ):
                                    event["deltaX"] = previous.get(
                                        "deltaX", 0
                                    ) + event.get("deltaX", 0)
                                    event["deltaY"] = previous.get(
                                        "deltaY", 0
                                    ) + event.get("deltaY", 0)
                                pending_mouse[event["type"]] = event
                                mouse_changed.set()
                            else:
                                pending_mouse.pop("mouseMoved", None)
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
                asyncio.create_task(mouse_commands()),
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
        if self.closed:
            return
        self.closed = True
        for task in list(self.acks):
            task.cancel()
        await asyncio.gather(*self.acks, return_exceptions=True)
        if self.navigation:
            self.navigation.cancel()
            await asyncio.gather(self.navigation, return_exceptions=True)
        if self.peer:
            with contextlib.suppress(Exception):
                await self.peer.close()
        if self.vnc_peer:
            with contextlib.suppress(Exception):
                await self.vnc_peer.close(code=4004)
        self.host.handlers.pop(self.cdp_session, None)
        with contextlib.suppress(Exception):
            await self.host.send("Target.closeTarget", {"targetId": self.target})

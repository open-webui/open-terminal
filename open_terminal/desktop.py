"""One on-demand desktop per workspace, shared by the viewer and model tools."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import secrets
import shutil
import sys
import importlib.util
import signal
import tempfile
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from open_terminal.dependencies import get_filesystem, matches_api_key, verify_api_key
from open_terminal.env import BROWSER_NO_SANDBOX, sanitized_environ
from open_terminal.utils.fs import UserFS

router = APIRouter(prefix="/desktop", tags=["desktop"])
WIDTH, HEIGHT = 1280, 800
desktops = {}
startup_lock = asyncio.Lock()
log = logging.getLogger(__name__)


def available():
    if sys.platform in ("darwin", "win32"):
        return all(
            importlib.util.find_spec(name) for name in ("mss", "pyautogui", "pyperclip")
        )
    return all(
        shutil.which(name)
        for name in (
            "Xtigervnc",
            "startxfce4",
            "dbus-run-session",
            "xauth",
            "xdotool",
            "scrot",
            "xclip",
        )
    )


class Desktop:
    transport = "vnc"
    width, height = WIDTH, HEIGHT

    def __init__(self, fs):
        self.fs = fs
        self.runtime = Path(tempfile.mkdtemp(prefix="open-terminal-desktop-"))
        self.processes = []
        self.audio_process = None
        self.actions = asyncio.Lock()
        self.peers = set()
        self.env = {
            "HOME": fs.home,
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XAUTHORITY": str(self.runtime / "Xauthority"),
        }
        if BROWSER_NO_SANDBOX:
            self.env["OPEN_TERMINAL_BROWSER_NO_SANDBOX"] = "true"

    async def spawn(self, *command, **kwargs):
        if self.fs.username:
            command = (
                "sudo",
                "-H",
                "-n",
                "-u",
                self.fs.username,
                "--",
                "env",
                *(f"{k}={v}" for k, v in self.env.items()),
                *command,
            )
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=self.fs.home,
            env=sanitized_environ(self.env),
            start_new_session=True,
            **kwargs,
        )

    async def stop_process(self, process):
        if process.returncode is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self.fs.username and os.getuid() != 0:
                killer = await asyncio.create_subprocess_exec(
                    "sudo",
                    "-n",
                    "kill",
                    f"-{sig.name[3:]}",
                    "--",
                    f"-{process.pid}",
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, sig)
            try:
                await asyncio.wait_for(process.wait(), 3)
                return
            except TimeoutError:
                pass

    async def run(self, *command, input=None):
        # xclip forks a clipboard owner; inherited pipes would never reach EOF.
        output = (
            asyncio.subprocess.DEVNULL
            if command[0] == "xclip"
            else asyncio.subprocess.PIPE
        )
        process = await self.spawn(
            *command,
            stdin=asyncio.subprocess.PIPE
            if input is not None
            else asyncio.subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), 10)
            if process.returncode:
                raise RuntimeError(
                    (stderr or b"").decode(errors="replace")[-1000:]
                    or "Desktop command failed"
                )
            return stdout or b""
        finally:
            await self.stop_process(process)

    async def start(self, *, shell=True):
        if self.fs.username:
            import pwd

            account = pwd.getpwnam(self.fs.username)
            os.chmod(self.runtime, 0o770)
            process = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                "chown",
                f"{account.pw_uid}:{os.getegid()}",
                str(self.runtime),
            )
            if await process.wait():
                raise RuntimeError("Could not create desktop runtime directory")
        display = next(
            n
            for n in range(40, 1040)
            if not Path(f"/tmp/.X{n}-lock").exists()
            and not Path(f"/tmp/.X11-unix/X{n}").exists()
        )
        self.env["DISPLAY"] = f":{display}"
        await self.run(
            "xauth",
            "-f",
            self.env["XAUTHORITY"],
            "add",
            self.env["DISPLAY"],
            ".",
            secrets.token_hex(16),
        )
        # VNC is reachable only through the authenticated WebSocket relay.
        with (self.runtime / "display.log").open("wb") as log:
            server = await self.spawn(
                "Xtigervnc",
                self.env["DISPLAY"],
                "-geometry",
                f"{WIDTH}x{HEIGHT}",
                "-depth",
                "24",
                "-rfbport",
                "-1",
                "-rfbunixpath",
                str(self.runtime / "vnc.sock"),
                "-rfbunixmode",
                "0660",
                "-SecurityTypes",
                "None",
                "-AlwaysShared",
                "-AcceptSetDesktopSize=0",
                "-nolisten",
                "tcp",
                "-auth",
                self.env["XAUTHORITY"],
                "-desktop",
                "Desktop",
                stdout=log,
                stderr=log,
            )
        self.processes.append(server)
        for _ in range(100):
            if server.returncode is not None:
                raise RuntimeError((self.runtime / "display.log").read_text()[-1000:])
            if (self.runtime / "vnc.sock").exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Desktop display did not start")
        if not shell:
            return
        await self.start_audio()
        with (self.runtime / "session.log").open("wb") as log:
            self.processes.append(
                await self.spawn(
                    "dbus-run-session", "--", "startxfce4", stdout=log, stderr=log
                )
            )
        await self.run(
            "xdotool", "search", "--sync", "--onlyvisible", "--class", "xfdesktop"
        )

    @property
    def audio_available(self):
        return self.audio_process is not None and self.audio_process.returncode is None

    async def start_audio(self):
        if not all(shutil.which(name) for name in ("pulseaudio", "ffmpeg", "pactl")):
            return
        socket = self.runtime / "audio.sock"
        self.env.update(
            PULSE_SERVER=f"unix:{socket}",
            PULSE_SINK="desktop",
            PULSE_SOURCE="desktop.monitor",
        )
        try:
            with (self.runtime / "audio.log").open("wb") as output:
                self.audio_process = await self.spawn(
                    "pulseaudio",
                    "--daemonize=no",
                    "--use-pid-file=no",
                    "--exit-idle-time=-1",
                    "--disable-shm=yes",
                    "--log-target=stderr",
                    "-n",
                    "--load=module-null-sink sink_name=desktop rate=48000 channels=2",
                    # Only this workspace's Unix socket; clients run as its OS user.
                    f"--load=module-native-protocol-unix socket={socket} auth-cookie-enabled=0",
                    stdout=output,
                    stderr=output,
                )
            for _ in range(50):
                if self.audio_process.returncode is not None:
                    raise RuntimeError("Desktop audio server exited")
                if socket.exists():
                    await self.run("pactl", "info")
                    return
                await asyncio.sleep(0.1)
            raise RuntimeError("Desktop audio server did not start")
        except (OSError, RuntimeError, TimeoutError):
            log.warning(
                "Desktop audio unavailable; continuing without sound", exc_info=True
            )
            if self.audio_process:
                await self.stop_process(self.audio_process)
            self.audio_process = None
            for key in ("PULSE_SERVER", "PULSE_SINK", "PULSE_SOURCE"):
                self.env.pop(key, None)

    async def stream_audio(self, ws):
        process = await self.spawn(
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-f", "pulse", "-sample_rate", "48000", "-channels", "2",
            "-fragment_size", "3840", "-i", "desktop.monitor",
            "-c:a", "libopus", "-b:a", "64k", "-vbr", "off",
            "-application", "lowdelay", "-frame_duration", "20",
            "-f", "webm", "-live", "1", "-cluster_time_limit", "100",
            "-flush_packets", "1", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.peers.add(ws)

        async def outgoing():
            # Forward the WebM stream unchanged; the browser handles Opus decoding.
            while data := await process.stdout.read(4096):
                await asyncio.wait_for(ws.send_bytes(data), 2)

        async def incoming():
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.peers.discard(ws)
            await self.stop_process(process)

    async def screenshot(self):
        path = self.runtime / "screenshot.png"
        await self.run("scrot", "--overwrite", str(path))
        return await asyncio.to_thread(path.read_bytes)

    async def stream(self, ws):
        reader, writer = await asyncio.open_unix_connection(
            str(self.runtime / "vnc.sock")
        )
        self.peers.add(ws)

        async def incoming():
            while True:
                writer.write(await ws.receive_bytes())
                await writer.drain()

        async def outgoing():
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.peers.discard(ws)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        for peer in list(self.peers):
            with contextlib.suppress(Exception):
                await peer.close(code=1012)
        for process in reversed(self.processes):
            await self.stop_process(process)
        if self.audio_process:
            await self.stop_process(self.audio_process)
        if self.fs.username:
            await self.run("rm", "-rf", "--", str(self.runtime))
        else:
            shutil.rmtree(self.runtime, ignore_errors=True)


async def get_desktop(request, fs):
    if not available():
        raise HTTPException(503, "Desktop is not installed on this terminal")
    if sys.platform in ("darwin", "win32"):
        from open_terminal.desktop_native import check_permissions

        check_permissions()
    key = (request.headers.get("x-user-id", ""), fs.home)
    async with startup_lock:
        desktop = desktops.get(key)
        if desktop and all(p.returncode is None for p in desktop.processes):
            return desktop
        if desktop:
            await desktop.close()
            desktops.pop(key, None)
        if sys.platform in ("darwin", "win32"):
            from open_terminal.desktop_native import NativeDesktop

            desktop = NativeDesktop(fs)
        else:
            desktop = Desktop(fs)
        try:
            await desktop.start()
        except BaseException:
            await desktop.close()
            raise
        desktops[key] = desktop
        return desktop


async def shutdown():
    await asyncio.gather(
        *(desktop.close() for desktop in desktops.values()), return_exceptions=True
    )
    desktops.clear()


@router.get("", include_in_schema=False, dependencies=[Depends(verify_api_key)])
async def connect(request: Request, fs: UserFS = Depends(get_filesystem)):
    desktop = await get_desktop(request, fs)
    return {
        "width": desktop.width,
        "height": desktop.height,
        "transport": desktop.transport,
        "platform": sys.platform,
        "audio": getattr(desktop, "audio_available", False),
    }


@router.get(
    "/screenshot",
    operation_id="desktop_screenshot",
    summary="See the workspace desktop",
    dependencies=[Depends(verify_api_key)],
)
async def screenshot(request: Request, fs: UserFS = Depends(get_filesystem)):
    """Return an image of the same desktop the user sees. Starts it if needed.
    Coordinates are pixels in the returned image. Inspect before interacting.
    On macOS and Windows this is the logged-in user's primary screen, shared across workspaces.
    """
    desktop = await get_desktop(request, fs)
    async with desktop.actions:
        return Response(await desktop.screenshot(), media_type="image/png")


ActionType = Literal["click", "double_click", "move", "drag", "scroll", "type", "key"]


class Action(BaseModel):
    model_config = {"extra": "forbid"}

    action: ActionType
    x: int | None = Field(default=None, ge=0, le=32767)
    y: int | None = Field(default=None, ge=0, le=32767)
    to_x: int | None = Field(default=None, ge=0, le=32767)
    to_y: int | None = Field(default=None, ge=0, le=32767)
    button: Literal["left", "middle", "right"] = "left"
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=20)
    text: str | None = Field(default=None, max_length=20000)
    key: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_+]+$",
        max_length=100,
        description="Key or combination, e.g. Return, ctrl+c, command+c on macOS, alt+F2 on Linux",
    )

    @model_validator(mode="after")
    def required_fields(self):
        if self.action in ("click", "double_click", "move", "drag") and (
            self.x is None or self.y is None
        ):
            raise ValueError("This action requires x and y")
        if (self.x is None) != (self.y is None):
            raise ValueError("Provide both x and y")
        if self.action == "drag" and (self.to_x is None or self.to_y is None):
            raise ValueError("Drag requires to_x and to_y")
        if self.action == "type" and self.text is None:
            raise ValueError("Type requires text")
        if self.action == "key" and not self.key:
            raise ValueError("Key requires a key combination")
        return self


class ActionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    actions: list[Action] = Field(
        min_length=1,
        max_length=50,
        description="Desktop actions to execute in order, with one final screenshot.",
    )


async def perform_action(desktop, body):
    if desktop.transport == "frames":
        try:
            await asyncio.to_thread(desktop.perform, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await asyncio.sleep(0.1)
        return
    button = str({"left": 1, "middle": 2, "right": 3}[body.button])
    if body.x is not None:
        await desktop.run(
            "xdotool", "mousemove", "--sync", str(body.x), str(body.y)
        )
    if body.action in ("click", "double_click"):
        await desktop.run(
            "xdotool",
            "click",
            "--repeat",
            "2" if body.action == "double_click" else "1",
            "--delay",
            "100",
            button,
        )
    elif body.action == "drag":
        try:
            await desktop.run(
                "xdotool",
                "mousedown",
                button,
                "mousemove",
                "--sync",
                str(body.to_x),
                str(body.to_y),
            )
        finally:
            await desktop.run("xdotool", "mouseup", button)
    elif body.action == "scroll":
        await desktop.run(
            "xdotool",
            "click",
            "--repeat",
            str(body.amount),
            "--delay",
            "50",
            str({"up": 4, "down": 5, "left": 6, "right": 7}[body.direction]),
        )
    elif body.action == "type":
        for selection in ("clipboard", "primary"):
            await desktop.run(
                "xclip", "-selection", selection, input=body.text.encode()
            )
        await desktop.run("xdotool", "key", "--clearmodifiers", "shift+Insert")
    elif body.action == "key":
        await desktop.run("xdotool", "key", "--clearmodifiers", body.key)
    await asyncio.sleep(0.15)


@router.post(
    "/action",
    operation_id="desktop_action",
    summary="Interact with the workspace desktop",
    dependencies=[Depends(verify_api_key)],
)
async def action(
    request: Request, body: ActionRequest, fs: UserFS = Depends(get_filesystem)
):
    """Batch known steps in one call; stop when you need a fresh observation.
    Executes 1–50 actions in order and returns one final screenshot.
    Each item can click, double-click, move, drag, scroll, type, or press a key.
    Use desktop_screenshot first and work from that image's pixel coordinates.
    Native macOS/Windows share the logged-in desktop. Type pastes literal text.
    Batches stop on failure and report completed_actions and failed_action_index
    (zero-based, null if only the final screenshot failed). Completed actions are
    not rolled back; the failed action may also have taken effect. Inspect the
    desktop before retrying, and do not blindly replay the batch.
    """
    desktop = await get_desktop(request, fs)
    actions = body.actions
    async with desktop.actions:
        # Reject invalid coordinates anywhere in the batch before sending input.
        for item in actions:
            for x, y in ((item.x, item.y), (item.to_x, item.to_y)):
                if x is not None and (
                    x >= desktop.width or y is None or y >= desktop.height
                ):
                    raise HTTPException(
                        400, "Coordinates are outside the current desktop screenshot"
                    )
        completed = 0
        try:
            for item in actions:
                await perform_action(desktop, item)
                completed += 1
            return Response(await desktop.screenshot(), media_type="image/png")
        except (HTTPException, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                exc.status_code if isinstance(exc, HTTPException) else 502,
                {
                    "completed_actions": completed,
                    "failed_action_index": completed if completed < len(actions) else None,
                    "error": exc.detail if isinstance(exc, HTTPException) else str(exc),
                },
            ) from exc


@router.websocket("/audio")
@router.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 10)
        if (
            not isinstance(auth, dict)
            or auth.get("type") != "auth"
            or not matches_api_key(auth.get("token"))
        ):
            await ws.close(code=4001)
            return
        desktop = await get_desktop(ws, get_filesystem(ws))
        if ws.url.path.endswith("/audio"):
            if not getattr(desktop, "audio_available", False):
                await ws.close(code=4004, reason="Desktop audio unavailable")
                return
            await desktop.stream_audio(ws)
        else:
            await desktop.stream(ws)
    except (WebSocketDisconnect, TimeoutError, OSError, ValueError, HTTPException):
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close(code=1011)

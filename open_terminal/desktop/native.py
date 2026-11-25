"""Direct host capture for macOS, Windows, and Linux/X11.

Linux/Wayland host capture and control use the portal backend.
"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import ctypes
import contextlib
import io
import logging
import shutil
import subprocess
import sys
import time

from fastapi import HTTPException, WebSocketDisconnect
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from open_terminal.frames import Frames, FrameWindow
from open_terminal.desktop.audio import HostAudio
from open_terminal.desktop.video import VideoEncoder
from open_terminal.env import sanitized_environ

log = logging.getLogger(__name__)


class Input(BaseModel):
    type: Literal["mouse", "key", "paste", "release", "frameAck"]
    window: int = Field(default=1, ge=1, le=3)
    h264: bool = False
    x: int = Field(default=0, ge=0, le=32767)
    y: int = Field(default=0, ge=0, le=32767)
    buttons: int = Field(default=0, ge=0, le=7)
    deltaX: int = Field(default=0, ge=-10000, le=10000)
    deltaY: int = Field(default=0, ge=-10000, le=10000)
    scrollX: float = Field(default=0, ge=-100000, le=100000)
    scrollY: float = Field(default=0, ge=-100000, le=100000)
    code: str = Field(default="", max_length=50)
    down: bool = False
    text: str = Field(default="", max_length=20000)
    shift: bool = False


KEYS = {
    "Enter": "enter",
    "NumpadEnter": "enter",
    "Escape": "esc",
    "Space": "space",
    "Backspace": "backspace",
    "Tab": "tab",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ShiftLeft": "shiftleft",
    "ShiftRight": "shiftright",
    "ControlLeft": "ctrlleft",
    "ControlRight": "ctrlright",
    "AltLeft": "altleft",
    "AltRight": "altright",
    "MetaLeft": "command" if sys.platform == "darwin" else "winleft",
    "MetaRight": "command" if sys.platform == "darwin" else "winright",
    "CapsLock": "capslock",
    "Minus": "-",
    "Equal": "=",
    "BracketLeft": "[",
    "BracketRight": "]",
    "Backslash": "\\",
    "Semicolon": ";",
    "Quote": "'",
    "Comma": ",",
    "Period": ".",
    "Slash": "/",
    "Backquote": "`",
    **{f"Key{chr(n)}": chr(n).lower() for n in range(65, 91)},
    **{f"Digit{n}": str(n) for n in range(10)},
    **{f"F{n}": f"f{n}" for n in range(1, 25)},
}
ALIASES = {
    "return": "enter",
    "escape": "esc",
    "super_l": "command" if sys.platform == "darwin" else "win",
    "cmd": "command",
    "control": "ctrl",
    "page_up": "pageup",
    "page_down": "pagedown",
}


def check_permissions():
    if sys.platform == "darwin":
        graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        access = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        access.AXIsProcessTrusted.restype = ctypes.c_bool
        missing = []
        if not graphics.CGPreflightScreenCaptureAccess():
            missing.append("Screen Recording")
        if not access.AXIsProcessTrusted():
            missing.append("Accessibility")
        if missing:
            raise HTTPException(
                403,
                "Allow "
                + " and ".join(missing)
                + " for the app running Open Terminal in System Settings → Privacy & Security, then restart Open Terminal.",
            )


class NativeDesktop:
    transport = "frames"
    fps = 30
    draws_cursor = False

    def __init__(self, fs):
        if fs.username:
            raise HTTPException(
                403,
                "Native desktop requires Open Terminal to run as the logged-in desktop user.",
            )
        self.actions = asyncio.Lock()
        self.processes = []
        self.peers = set()
        self.env = {}
        self.audio = HostAudio(self)
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="desktop-capture"
        )
        # Input must not wait behind frame capture and encoding.
        self.inputs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-input")
        self.capture = None
        self.origin = (0, 0)
        self.target = ()
        self.width = self.height = 0
        self.frame = None
        self.latest = None
        self.screen = self.screen_task = None
        self.raw = self.basic = self.plain = None
        self.shared = None
        self.shared_at = 0.0
        self.unscrolled = [0.0, 0.0]

    @property
    def frame_size(self):
        """The viewer's picture; assistants always use width and height."""
        return self.frame or (self.width, self.height)

    async def work(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.worker, fn, *args)

    async def send_input(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.inputs, fn, *args)

    def setup(self):
        check_permissions()
        import pyautogui

        self.input = pyautogui
        self.input.PAUSE = 0
        if sys.platform == "darwin":
            self.input.DARWIN_CATCH_UP_TIME = 0.001
        # A remote viewer must be able to use screen corners, like the VNC viewer.
        self.input.FAILSAFE = False
        if sys.platform == "linux":
            self.open_capture()
        else:
            self.measure(*self.input.size())

    def open_capture(self):
        import mss

        self.capture = mss.mss()
        self.grab()
        if sys.platform == "linux":
            self.plain, self.capture = self.capture, mss.mss(with_cursor=True)
            try:
                self.capture.grab(self.capture.monitors[1])
                self.fps, self.draws_cursor = 60, True
            except mss.ScreenShotError:  # No XFixes; viewers show their own cursor.
                self.capture.close()
                self.capture, self.plain = self.plain, None

    def screen_command(self):
        """A process that writes raw BGRX frames, only when the screen changes."""
        size = (str(self.width), str(self.height))
        if sys.platform == "darwin":
            return (sys.executable, "-u", "-m", "open_terminal.desktop.screen", *size, "60")
        # Desktop Duplication numbers displays itself, so use it only when there is one.
        if sys.platform == "win32" and shutil.which("ffmpeg") and ctypes.windll.user32.GetSystemMetrics(80) == 1:
            return (
                "ffmpeg", "-nostdin", "-loglevel", "error", "-filter_complex",
                "ddagrab=framerate=60:dup_frames=0,hwdownload,format=bgra,"
                f"scale={size[0]}:{size[1]}:flags=bilinear,format=bgr0",
                "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
            )
        return None

    def measure(self, width, height):
        self.screen_width, self.screen_height = width, height
        scale = min(1, 1920 / width, 1080 / height)
        self.width, self.height = round(width * scale), round(height * scale)

    async def start_screen(self):
        command = self.screen_command()
        if command is None:
            raise RuntimeError("needs one display and FFmpeg 7")
        self.screen = await self.spawn(*command, stdout=asyncio.subprocess.PIPE)
        data = await asyncio.wait_for(self.read_screen(), 15)
        self.update_frame(data)
        self.screen_started = time.monotonic()
        self.fps, self.draws_cursor = 60, True

    async def read_screen(self):
        return await self.screen.stdout.readexactly(self.width * self.height * 4)

    def update_frame(self, data):
        self.latest = Image.frombytes("RGB", (self.width, self.height), data, "raw", "BGRX")

    async def follow_screen(self):
        try:
            while True:
                try:
                    while True:
                        self.update_frame(await self.read_screen())
                except asyncio.IncompleteReadError:
                    size = await self.work(self.input.size)
                    unchanged = tuple(size) == (self.screen_width, self.screen_height)
                    if unchanged and time.monotonic() - self.screen_started < 5:
                        raise
                    await self.stop_process(self.screen)
                    self.measure(*size)
                    await self.start_screen()
        except Exception as exc:
            log.warning("Screen capture stopped, using basic capture: %s", str(exc) or type(exc).__name__)
            await self.stop_process(self.screen)
            self.latest = None
            self.fps, self.draws_cursor = type(self).fps, False
            # Viewers reconnect, to show their own cursor again.
            for peer in list(self.peers):
                with contextlib.suppress(Exception):
                    await peer.close(code=1012)

    async def start(self):
        try:
            await self.work(self.setup)
            if sys.platform != "linux":
                try:
                    await self.start_screen()
                    self.screen_task = asyncio.create_task(self.follow_screen())
                except Exception as exc:
                    log.warning("Screen capture unavailable, using basic capture: %s", str(exc) or type(exc).__name__)
                    if self.screen:
                        await self.stop_process(self.screen)
                    await self.work(self.open_capture)
            await self.audio.start()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                503,
                "Desktop capture requires a logged-in, unlocked graphical session: "
                + str(exc),
            ) from exc

    @property
    def audio_available(self):
        return self.audio.available

    async def stream_audio(self, ws):
        await self.audio.stream(ws)

    async def spawn(self, *command, **kwargs):
        env = sanitized_environ(self.env)
        env.pop("SSH_CONNECTION", None)
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return await asyncio.create_subprocess_exec(*command, env=env, **kwargs)

    async def stop_process(self, process):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    def grab(self):
        if (latest := self.latest) is not None:
            return latest
        if self.capture is None:
            self.open_capture()
        screen = next(
            (m for m in self.capture.monitors[1:] if m["left"] == 0 and m["top"] == 0),
            self.capture.monitors[1],
        )
        try:
            shot = self.capture.grab(screen)
        except Exception:
            if self.plain is None:
                raise
            shot = self.plain.grab(screen)  # The app that set this cursor has gone.
        if self.basic is not None and shot.raw == self.raw:
            return self.basic  # Unchanged, so there is nothing to compare or encode.
        self.raw = shot.raw
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        self.origin = (screen["left"], screen["top"]) if sys.platform == "linux" else (0, 0)
        # X11's root size spans every monitor; input must match the captured one.
        self.measure(*(
            (screen["width"], screen["height"])
            if sys.platform == "linux" else self.input.size()
        ))
        self.basic = image.resize((self.width, self.height), Image.Resampling.BILINEAR)
        return self.basic

    def next_frame(self, last):
        """Grab the screen, or reuse a fresh grab this viewer has not had yet."""
        if self.latest is not None:
            return self.grab()  # Already one picture for everyone.
        now = time.monotonic()
        if self.shared is None or self.shared is last or now - self.shared_at > 1 / self.fps:
            self.shared, self.shared_at = self.grab(), now
        return self.shared

    def position(self, x, y, buttons=(), size=None):
        width, height = size or (self.width, self.height)
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("Coordinates are outside the current desktop screenshot")
        # Button and scroll calls pass this on: macOS applies moves asynchronously,
        # so reading the cursor back can return where it was before the move.
        self.target = (
            self.origin[0] + round(x * self.screen_width / width),
            self.origin[1] + round(y * self.screen_height / height),
        )
        if sys.platform == "darwin" and buttons:
            # macOS requires drag events, even when mouseDown has already been sent.
            button = next(b for b in ("left", "right", "middle") if b in buttons)
            self.input.dragTo(*self.target, button=button, mouseDownUp=False)
        else:
            self.input.moveTo(*self.target)

    def scroll(self, x, y):
        """Scroll by screen pixels, keeping what is too small to send yet."""
        unit = {"darwin": 1, "win32": 100 / 120}.get(sys.platform, 100)
        steps = []
        for axis, amount in enumerate((x, y)):
            total = self.unscrolled[axis] + amount
            steps.append(int(total / unit))
            self.unscrolled[axis] = total - steps[axis] * unit
        x, y = steps
        if sys.platform == "darwin" and (x or y):
            import Quartz

            event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2, -y, -x)
            Quartz.CGEventSetLocation(event, self.target)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        elif sys.platform == "win32":
            # pyautogui scrolls 1/120 of a notch per step and has no horizontal wheel.
            for event, amount in ((0x0800, -y), (0x1000, x)):
                if amount:
                    ctypes.windll.user32.mouse_event(event, 0, 0, amount & 0xFFFFFFFF, 0)
        else:
            if y:
                self.input.scroll(-y, *self.target)
            if x:
                self.input.hscroll(x, *self.target)

    def paste(self, text, shift=False):
        import pyperclip

        pyperclip.copy(text)
        self.input.hotkey("command" if sys.platform == "darwin" else "ctrl", *(["shift"] if shift else []), "v")

    def release(self, keys, buttons):
        for key in list(keys):
            self.input.keyUp(key)
        keys.clear()
        for button in list(buttons):
            self.input.mouseUp(*self.target, button=button)
        buttons.clear()

    def handle(self, message, keys, buttons):
        if message.type == "mouse":
            width, height = self.frame_size
            self.position(message.x, message.y, buttons, (width, height))
            for flag, button in ((1, "left"), (2, "right"), (4, "middle")):
                down = bool(message.buttons & flag)
                if down and button not in buttons:
                    self.input.mouseDown(*self.target, button=button)
                    buttons.add(button)
                elif not down and button in buttons:
                    self.input.mouseUp(*self.target, button=button)
                    buttons.remove(button)
            if message.scrollX or message.scrollY:
                self.scroll(
                    message.scrollX * self.screen_width / width,
                    message.scrollY * self.screen_height / height,
                )
                return
            if message.deltaY:
                self.input.scroll(-message.deltaY, *self.target)
            if message.deltaX:
                self.input.hscroll(message.deltaX, *self.target)
        elif message.type == "key" and (key := KEYS.get(message.code)):
            if message.down:
                self.input.keyDown(key)
                keys.add(key)
            else:
                self.input.keyUp(key)
                keys.discard(key)
        elif message.type == "paste":
            self.release(keys, buttons)
            self.paste(message.text, message.shift)
        elif message.type == "release":
            self.release(keys, buttons)

    def perform(self, body):
        if body.x is not None:
            self.position(body.x, body.y)
        if body.action in ("click", "double_click"):
            self.input.click(
                *self.target,
                button=body.button,
                clicks=2 if body.action == "double_click" else 1,
                interval=0.1 if body.action == "double_click" else 0,
            )
        elif body.action == "drag":
            try:
                self.input.mouseDown(*self.target, button=body.button)
                self.position(body.to_x, body.to_y, (body.button,))
            finally:
                self.input.mouseUp(*self.target, button=body.button)
        elif body.action == "scroll":
            amount = body.amount * (-1 if body.direction in ("down", "left") else 1)
            (
                self.input.hscroll
                if body.direction in ("left", "right")
                else self.input.scroll
            )(amount, *(self.target if body.x is not None else ()))
        elif body.action == "type":
            self.paste(body.text)
        elif body.action == "key":
            keys = [
                ALIASES.get(key.lower(), key.lower()) for key in body.key.split("+")
            ]
            if any(key not in self.input.KEYBOARD_KEYS for key in keys):
                raise ValueError("Unknown key combination")
            self.input.hotkey(*keys)

    async def screenshot(self):
        def encode():
            output = io.BytesIO()
            image = self.grab()
            if image.size != (self.width, self.height):
                image = image.resize((self.width, self.height), Image.Resampling.LANCZOS)
            image.save(output, "PNG")
            return output.getvalue()

        return await self.work(encode)

    async def stream(self, ws):
        self.peers.add(ws)
        ready = FrameWindow()
        keys, buttons = set(), set()
        encoder = Frames()
        video = VideoEncoder()
        # Each viewer encodes on its own thread, so one does not wait for another.
        encoding = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-encode")
        h264 = False  # The viewer says with each acknowledgment whether it decodes video.
        pending = deque()
        input_ready = asyncio.Event()

        def encode(image, level):
            if not h264 and video.process is not None:
                # The viewer stopped decoding, so its picture may be out of date.
                video.close()
                encoder.reset()
            return encoder.packets(image, level, video if h264 else None)

        async def frames():
            image = None
            while True:
                await ready.wait()
                start = time.monotonic()
                # A slow viewer gets lighter frames; still areas are sharpened afterwards.
                image = await self.work(self.next_frame, image)
                packets = await asyncio.get_running_loop().run_in_executor(
                    encoding, encode, image, ready.adapt()
                )
                if packets:
                    ready.sent(len(packets))
                    for packet in packets:
                        await ws.send_bytes(packet)
                fps = self.fps if ready.queued < 0.03 else 30
                await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - start)))

        async def commands():
            nonlocal h264
            received_buttons = 0
            while True:
                data = await ws.receive_json()
                try:
                    message = Input.model_validate(data)
                except ValidationError as exc:
                    log.warning("Ignored desktop input: %s", exc.errors()[0]["msg"])
                    continue
                if message.type == "frameAck":
                    h264 = message.h264
                    ready.ack(message.window)
                else:
                    motion = (
                        message.type == "mouse"
                        and message.buttons == received_buttons
                    )
                    scroll = bool(
                        message.deltaX or message.deltaY or message.scrollX or message.scrollY
                    )
                    if message.type == "mouse":
                        received_buttons = message.buttons
                    elif message.type in ("release", "paste"):
                        received_buttons = 0
                    # Merge consecutive moves and scrolls, never a button edge or key.
                    # A move after a scroll stays separate, so it cannot redirect it.
                    if motion and pending and pending[-1][1] and (scroll or not pending[-1][2]):
                        message.deltaX += pending[-1][0].deltaX
                        message.deltaY += pending[-1][0].deltaY
                        message.scrollX += pending[-1][0].scrollX
                        message.scrollY += pending[-1][0].scrollY
                        pending[-1] = (message, motion, scroll)
                    else:
                        if len(pending) >= 256:
                            raise ValueError("Desktop input queue is full")
                        pending.append((message, motion, scroll))
                    input_ready.set()

        async def inputs():
            while True:
                await input_ready.wait()
                message, *_ = pending.popleft()
                if not pending:
                    input_ready.clear()
                try:
                    async with self.actions:
                        await self.send_input(self.handle, message, keys, buttons)
                except Exception as exc:
                    if getattr(self, "closed", False):
                        raise
                    log.warning("Desktop input failed: %s", exc)

        tasks = [
            asyncio.create_task(frames()),
            asyncio.create_task(commands()),
            asyncio.create_task(inputs()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(video.close)
            encoding.shutdown(wait=False, cancel_futures=True)
            async with self.actions:
                with contextlib.suppress(Exception):
                    await self.send_input(self.release, keys, buttons)
            self.peers.discard(ws)

    async def close(self):
        if self.screen_task:
            self.screen_task.cancel()
            await asyncio.gather(self.screen_task, return_exceptions=True)
        if self.screen:
            await self.stop_process(self.screen)
        await self.audio.close()
        for peer in list(self.peers):
            with contextlib.suppress(Exception):
                await peer.close(code=1012)
        for capture in filter(None, (self.capture, self.plain)):
            await self.work(capture.close)
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.inputs.shutdown(wait=False, cancel_futures=True)

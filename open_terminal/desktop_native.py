"""Capture and control the logged-in macOS or Windows desktop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes
import io
import sys
import time

from fastapi import HTTPException, WebSocketDisconnect
from PIL import Image
from pydantic import BaseModel, Field
from typing import Literal

from open_terminal.frames import Frames


class Input(BaseModel):
    type: Literal["mouse", "key", "paste", "release", "frameAck"]
    x: int = Field(default=0, ge=0, le=32767)
    y: int = Field(default=0, ge=0, le=32767)
    buttons: int = Field(default=0, ge=0, le=7)
    deltaX: int = Field(default=0, ge=-10000, le=10000)
    deltaY: int = Field(default=0, ge=-10000, le=10000)
    code: str = Field(default="", max_length=50)
    down: bool = False
    text: str = Field(default="", max_length=20000)


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

    def __init__(self, fs):
        if fs.username:
            raise HTTPException(
                403,
                "Native desktop requires Open Terminal to run as the logged-in desktop user.",
            )
        self.actions = asyncio.Lock()
        self.processes = []
        self.peers = set()
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="desktop-capture"
        )
        self.capture = None
        self.width = self.height = 0

    async def work(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.worker, fn, *args)

    def setup(self):
        check_permissions()
        import mss
        import pyautogui

        self.input = pyautogui
        self.input.PAUSE = 0
        # A remote viewer must be able to use screen corners, like the VNC viewer.
        self.input.FAILSAFE = False
        self.capture = mss.mss()
        self.grab()

    async def start(self):
        try:
            await self.work(self.setup)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                503,
                "Desktop capture requires a logged-in, unlocked graphical session: "
                + str(exc),
            ) from exc

    def grab(self):
        screen = next(
            (m for m in self.capture.monitors[1:] if m["left"] == 0 and m["top"] == 0),
            self.capture.monitors[1],
        )
        shot = self.capture.grab(screen)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        self.screen_width, self.screen_height = self.input.size()
        scale = min(1, 1920 / self.screen_width, 1080 / self.screen_height)
        self.width, self.height = (
            round(self.screen_width * scale),
            round(self.screen_height * scale),
        )
        return image.resize((self.width, self.height), Image.Resampling.BILINEAR)

    def position(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError("Coordinates are outside the current desktop screenshot")
        self.input.moveTo(
            round(x * self.screen_width / self.width),
            round(y * self.screen_height / self.height),
        )

    def paste(self, text):
        import pyperclip

        pyperclip.copy(text)
        self.input.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")

    def release(self, keys, buttons):
        for key in list(keys):
            self.input.keyUp(key)
        keys.clear()
        for button in list(buttons):
            self.input.mouseUp(button=button)
        buttons.clear()

    def handle(self, message, keys, buttons):
        if message.type == "mouse":
            self.position(message.x, message.y)
            for flag, button in ((1, "left"), (2, "right"), (4, "middle")):
                down = bool(message.buttons & flag)
                if down and button not in buttons:
                    self.input.mouseDown(button=button)
                    buttons.add(button)
                elif not down and button in buttons:
                    self.input.mouseUp(button=button)
                    buttons.remove(button)
            if message.deltaY:
                self.input.scroll(-message.deltaY)
            if message.deltaX:
                self.input.hscroll(message.deltaX)
        elif message.type == "key" and (key := KEYS.get(message.code)):
            if message.down:
                self.input.keyDown(key)
                keys.add(key)
            else:
                self.input.keyUp(key)
                keys.discard(key)
        elif message.type == "paste":
            self.release(keys, buttons)
            self.paste(message.text)
        elif message.type == "release":
            self.release(keys, buttons)

    def perform(self, body):
        if body.x is not None:
            self.position(body.x, body.y)
        if body.action in ("click", "double_click"):
            self.input.click(
                button=body.button,
                clicks=2 if body.action == "double_click" else 1,
                interval=0.1 if body.action == "double_click" else 0,
            )
        elif body.action == "drag":
            try:
                self.input.mouseDown(button=body.button)
                self.position(body.to_x, body.to_y)
            finally:
                self.input.mouseUp(button=body.button)
        elif body.action == "scroll":
            amount = body.amount * (-1 if body.direction in ("down", "left") else 1)
            (
                self.input.hscroll
                if body.direction in ("left", "right")
                else self.input.scroll
            )(amount)
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
            self.grab().save(output, "PNG")
            return output.getvalue()

        return await self.work(encode)

    async def stream(self, ws):
        self.peers.add(ws)
        ready = asyncio.Event()
        ready.set()
        keys, buttons = set(), set()
        encoder = Frames()

        async def frames():
            while True:
                await ready.wait()
                start = time.monotonic()
                packet = await self.work(lambda: encoder.encode(self.grab()))
                if packet is not None:
                    ready.clear()
                    await ws.send_bytes(packet)
                await asyncio.sleep(max(0, 1 / 30 - (time.monotonic() - start)))

        async def commands():
            while True:
                message = Input.model_validate(await ws.receive_json())
                if message.type == "frameAck":
                    ready.set()
                else:
                    async with self.actions:
                        await asyncio.to_thread(self.handle, message, keys, buttons)

        tasks = [asyncio.create_task(frames()), asyncio.create_task(commands())]
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
            async with self.actions:
                await asyncio.to_thread(self.release, keys, buttons)
            self.peers.discard(ws)

    async def close(self):
        for peer in list(self.peers):
            await peer.close(code=1012)
        if self.capture:
            await self.work(self.capture.close)
        self.worker.shutdown(wait=False, cancel_futures=True)

"""Export owned Wayland frames through x11vnc's non-X raw framebuffer mode."""

import asyncio
import contextlib
import mmap
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile

from starlette.websockets import WebSocketDisconnect

from open_terminal.desktop.native import Input
from open_terminal.env import sanitized_environ


class VNCInput:
    def __init__(self, desktop):
        self.desktop = desktop
        self.keys = set()
        self.mask = 0

    async def handle(self, line):
        fields = line.split()
        if not fields or fields[0].startswith(b"#"):
            return
        if len(fields) != 6 or int(fields[1]) < 0:
            return
        notify = self.desktop.notify
        if fields[0] == b"Keysym":
            down, key = int(fields[2]), int(fields[3])
            if down:
                self.keys.add(key)
            else:
                self.keys.discard(key)
            await notify("NotifyKeyboardKeysym", "iu", key, down)
        elif fields[0] == b"Pointer":
            x, y, mask = map(int, fields[2:5])
            await notify("NotifyPointerMotionAbsolute", "udd", self.desktop.node,
                         float(min(max(x, 0), self.desktop.width - 1)),
                         float(min(max(y, 0), self.desktop.height - 1)))
            previous, self.mask = self.mask, mask
            for flag, button in ((1, 272), (2, 274), (4, 273), (128, 275)):
                if (mask ^ previous) & flag:
                    await notify("NotifyPointerButton", "iu", button, int(bool(mask & flag)))
            for flag, axis, steps in ((8, 0, -1), (16, 0, 1), (32, 1, -1), (64, 1, 1)):
                if mask & flag and not previous & flag:
                    await notify("NotifyPointerAxisDiscrete", "ui", axis, steps)

    async def release(self):
        for key in list(self.keys):
            with contextlib.suppress(Exception):
                await self.desktop.notify("NotifyKeyboardKeysym", "iu", key, 0)
        self.keys.clear()
        for flag, button in ((1, 272), (2, 274), (4, 273), (128, 275)):
            if self.mask & flag:
                with contextlib.suppress(Exception):
                    await self.desktop.notify("NotifyPointerButton", "iu", button, 0)
        self.mask = 0


class RawVNC:
    def __init__(self, desktop):
        self.desktop = desktop
        self.streams = set()
        self.path = desktop.runtime / "framebuffer"
        with self.path.open("w+b") as file:
            os.chmod(self.path, 0o600)
            file.truncate(desktop.width * desktop.height * 4)
            self.framebuffer = mmap.mmap(file.fileno(), 0)

    def update(self, data):
        if self.framebuffer is not None:
            self.framebuffer[:] = data

    async def stream(self, ws):
        desktop = self.desktop
        task = asyncio.current_task()
        self.streams.add(task)
        desktop.peers.add(ws)
        state = VNCInput(desktop)
        process = writer = pipe_transport = None
        tasks = []
        resources = contextlib.ExitStack()
        try:
            directory = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="vnc-", dir=desktop.runtime)))
            fifo, log = directory / "input", directory / "server.log"
            os.mkfifo(fifo, 0o600)
            # Hold the FIFO open until disconnect, including before the helper starts.
            pipe = resources.enter_context(os.fdopen(os.open(fifo, os.O_RDWR | os.O_NONBLOCK), "rb"))
            events = asyncio.StreamReader(limit=4096)
            pipe_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(events), pipe,
            )
            parent, child = socket.socketpair()
            resources.enter_context(parent)
            resources.enter_context(child)
            env = sanitized_environ(desktop.env)
            env = {k: v for k, v in env.items() if not k.startswith("X11VNC_")}
            env.pop("SSH_CONNECTION", None)
            masks = "ff/ff00/ff0000" if sys.byteorder == "little" else "ff000000/ff0000/ff00"
            process = await asyncio.create_subprocess_exec(
                "x11vnc", "-norc", "-inetd", "-nopw", "-quiet",
                "-rawfb", f"map:{self.path}@{desktop.width}x{desktop.height}x32:{masks}",
                "-pipeinput", f"cat > {shlex.quote(str(fifo))}",
                "-noremote", "-nosel", "-cursor", "none", "-no6", "-rfbport", "0",
                "-desktop", "Open Terminal GNOME", "-o", str(log),
                env=env, stdin=child, stdout=child, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            child.close()
            reader, writer = await asyncio.open_connection(sock=parent)

            async def incoming():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        writer.write(message["bytes"])
                        await writer.drain()
                    else:
                        command = Input.model_validate_json(message.get("text") or "")
                        if command.type != "paste":
                            raise ValueError("Only paste commands are supported alongside VNC")
                        async with desktop.actions:
                            await state.release()
                            await desktop.work(desktop.paste, command.text)

            async def outgoing():
                while data := await reader.read(65536):
                    await ws.send_bytes(data)

            async def inputs():
                while line := await events.readline():
                    async with desktop.actions:
                        await state.handle(line)

            tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, inputs)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
            if process.returncode not in (None, 0):
                raise RuntimeError("VNC server exited: " + log.read_text(errors="replace")[-2000:])
        except WebSocketDisconnect:
            pass
        finally:
            for child_task in tasks:
                child_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with desktop.actions:
                await state.release()
            if process is not None:
                await desktop.stop_process(process)
            if pipe_transport is not None:
                pipe_transport.close()
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            resources.close()
            desktop.peers.discard(ws)
            self.streams.discard(task)

    async def close(self):
        for task in list(self.streams):
            task.cancel()
        await asyncio.gather(*self.streams, return_exceptions=True)
        if self.framebuffer is not None:
            self.framebuffer.close()
            self.framebuffer = None

"""An owned, headless GNOME Wayland desktop, separate from the host session."""

import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import signal
import tempfile

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus
from fastapi import HTTPException

from open_terminal.desktop import WIDTH, HEIGHT
from open_terminal.desktop_portal import PortalDesktop, PortalInput
from open_terminal.env import sanitized_environ

REMOTE = "org.gnome.Mutter.RemoteDesktop"
SCREENCAST = "org.gnome.Mutter.ScreenCast"
PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"


class WaylandDesktop(PortalDesktop):
    def __init__(self, fs):
        if fs.username:
            raise HTTPException(503, "GNOME Wayland virtual desktops require single-user mode; use XFCE for multi-user deployments.")
        super().__init__(fs)
        self.runtime = Path(tempfile.mkdtemp(prefix="open-terminal-wayland-"))
        self.owner = None
        self.stream_path = None
        self.node_ready = asyncio.Event()
        self.width = self.screen_width = WIDTH
        self.height = self.screen_height = HEIGHT
        self.failure = "The virtual GNOME Wayland desktop has stopped. Reconnect to start a new session."
        # Every desktop service must use these private sockets, even when the
        # server was launched from an existing graphical or SSH session.
        self.env = {
            "HOME": fs.home,
            "XDG_RUNTIME_DIR": str(self.runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime / 'bus'}",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": "",
            "XAUTHORITY": "",
            "SESSION_MANAGER": "",
            "DBUS_STARTER_ADDRESS": "",
            "DBUS_STARTER_BUS_TYPE": "",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "GNOME",
            "GNOME_SHELL_SESSION_MODE": "user",
            "GDK_BACKEND": "wayland",
            "PIPEWIRE_RUNTIME_DIR": str(self.runtime),
            "PIPEWIRE_REMOTE": "pipewire-0",
            "PULSE_SERVER": f"unix:{self.runtime / 'pulse/native'}",
            "LIBGL_ALWAYS_SOFTWARE": "1",
        }

    @property
    def command_env(self):
        return self.env

    async def launch(self, name, *command):
        with (self.runtime / f"{name}.log").open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                *command, env=sanitized_environ(self.env), cwd=self.env["HOME"],
                start_new_session=True, stdout=output, stderr=output,
            )
        self.processes.append(process)
        return process

    async def wait_socket(self, name, process):
        for _ in range(100):
            if process.returncode is not None:
                raise RuntimeError(f"{name} service exited")
            if (self.runtime / name).is_socket():
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Timed out starting {name}")

    async def invoke(self, destination, path, interface, member, signature="", body=None):
        reply = await asyncio.wait_for(self.bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or [],
        )), 10)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"GNOME {member}: {reply.body[0] if reply.body else reply.error_name}")
        return reply.body

    async def bus_call(self, member, signature, body):
        return await self.invoke(DBUS, "/org/freedesktop/DBus", DBUS, member, signature, body)

    def on_message(self, message):
        if message.message_type != MessageType.SIGNAL or message.sender != self.owner:
            return
        if (message.interface == SCREENCAST + ".Stream"
                and message.member == "PipeWireStreamAdded" and message.path == self.stream_path):
            self.node = message.body[0]
            self.node_ready.set()
        elif message.interface == REMOTE + ".Session" and message.member == "Closed" and message.path == self.session:
            self.closed = True
            self.latest = None
            self.first_frame.set()

    async def start(self):
        self.loop = asyncio.get_running_loop()
        try:
            daemon = await self.launch(
                "dbus", "dbus-daemon", "--session", "--nofork",
                f"--address={self.env['DBUS_SESSION_BUS_ADDRESS']}",
            )
            await self.wait_socket("bus", daemon)
            pipewire = await self.launch("pipewire", "pipewire")
            await self.wait_socket("pipewire-0", pipewire)
            await self.launch("wireplumber", "wireplumber")
            compositor = await self.launch(
                "gnome", "gnome-shell", "--headless", "--wayland",
                f"--virtual-monitor={WIDTH}x{HEIGHT}", "--wayland-display=wayland-0",
            )
            self.bus = await asyncio.wait_for(
                MessageBus(bus_address=self.env["DBUS_SESSION_BUS_ADDRESS"]).connect(), 10
            )
            for _ in range(300):
                if compositor.returncode is not None:
                    raise RuntimeError("GNOME Shell exited")
                if (await self.bus_call("NameHasOwner", "s", [REMOTE]))[0]:
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("GNOME did not expose its remote desktop service")
            # Direct Mutter control is restricted to the compositor we launched.
            # Never use the host bus or activate/attach to another GNOME session.
            for name in (REMOTE, SCREENCAST):
                pid = (await self.bus_call("GetConnectionUnixProcessID", "s", [name]))[0]
                if pid != compositor.pid:
                    raise RuntimeError("GNOME service does not belong to the virtual compositor")
            self.owner = (await self.bus_call("GetNameOwner", "s", [REMOTE]))[0]
            self.bus.add_message_handler(self.on_message)
            await self.bus_call("AddMatch", "s", [f"type='signal',sender='{self.owner}'"])
            self.session = (await self.invoke(REMOTE, "/org/gnome/Mutter/RemoteDesktop", REMOTE, "CreateSession"))[0]
            session_id = (await self.invoke(REMOTE, self.session, PROPERTIES, "Get", "ss", [REMOTE + ".Session", "SessionId"]))[0].value
            cast = (await self.invoke(
                SCREENCAST, "/org/gnome/Mutter/ScreenCast", SCREENCAST, "CreateSession", "a{sv}",
                [{"remote-desktop-session-id": Variant("s", session_id)}],
            ))[0]
            self.stream_path = (await self.invoke(
                SCREENCAST, cast, SCREENCAST + ".Session", "RecordMonitor", "sa{sv}",
                ["", {"cursor-mode": Variant("u", 1)}],
            ))[0]
            self.input = PortalInput(self)
            await self.invoke(REMOTE, self.session, REMOTE + ".Session", "Start")
            await asyncio.wait_for(self.node_ready.wait(), 15)
            await self.start_capture(env=self.env)
        except Exception as exc:
            details = []
            for name in ("gnome", "pipewire", "dbus"):
                path = self.runtime / f"{name}.log"
                if path.exists():
                    details.append(f"{name}: {path.read_text(errors='replace')[-1500:]}")
            raise RuntimeError(f"Could not start virtual GNOME Wayland: {exc}\n" + "\n".join(details)) from exc

    async def notify(self, member, signature, *body):
        if self.closed:
            raise RuntimeError(self.failure)
        if member == "NotifyPointerMotionAbsolute":
            signature, body = "sdd", [self.stream_path, body[1], body[2]]
        elif member in ("NotifyKeyboardKeysym", "NotifyPointerButton"):
            signature = "ub" if member == "NotifyKeyboardKeysym" else "ib"
            body = [body[0], bool(body[1])]
        await self.invoke(REMOTE, self.session, REMOTE + ".Session", member, signature, list(body))

    async def stop_session(self):
        if self.bus and self.session:
            await self.invoke(REMOTE, self.session, REMOTE + ".Session", "Stop")

    async def stop_process(self, process):
        # Services can spawn D-Bus-activated apps in the same owned process group.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, sig)
            try:
                await asyncio.wait_for(process.wait(), 3)
                return
            except TimeoutError:
                pass

    async def close(self):
        try:
            await super().close()
        finally:
            shutil.rmtree(self.runtime, ignore_errors=True)

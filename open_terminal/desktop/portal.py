"""Share an existing Linux desktop through the consent-based XDG portal.

The portal owns capture/input permission. GStreamer reads only the PipeWire
stream it grants; no VNC/RDP listener or privileged input device is needed.
"""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import secrets
import tempfile
import time

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus
from fastapi import HTTPException
from PIL import Image

from open_terminal.desktop.native import NativeDesktop
from open_terminal.env import sanitized_environ

DESTINATION = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
SESSION = "org.freedesktop.portal.Session"
REQUEST = "org.freedesktop.portal.Request"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
DISPLAY_CONFIG = "org.gnome.Mutter.DisplayConfig"
log = logging.getLogger(__name__)


def restore_token_path():
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "open-terminal" / "desktop-restore-token"


def read_restore_token():
    try:
        with restore_token_path().open(encoding="utf-8") as source:
            token = source.read(4097).strip()
        return token if token and len(token) <= 4096 else None
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        log.warning("Could not read saved desktop authorization; approval will be requested again.")
        return None


def save_restore_token(token):
    path = restore_token_path()
    temporary = None
    try:
        if not token:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The grant is a credential. Replace atomically using a private file.
        fd, temporary = tempfile.mkstemp(prefix=".desktop-token-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(token)
        os.replace(temporary, path)
    except OSError:
        log.warning("Could not update saved desktop authorization; the next connection may require approval again.")
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def host_session():
    """Resolve the desktop user's bus without inheriting an SSH/private bus."""
    runtime = Path(f"/run/user/{os.getuid()}")
    socket = runtime / "bus"
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    if ssh or not address:
        if (not runtime.is_dir() or runtime.stat().st_uid != os.getuid()
                or not socket.is_socket() or socket.stat().st_uid != os.getuid()):
            raise RuntimeError(
                "No desktop user bus. Log in to Ubuntu's graphical desktop as "
                "the same OS user first; do not run Open Terminal with sudo."
            )
        address = f"unix:path={socket}"
        return address, {"XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": address}
    return address, {}

KEYSYMS = {
    "enter": 0xFF0D, "esc": 0xFF1B, "tab": 0xFF09, "backspace": 0xFF08,
    "delete": 0xFFFF, "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pageup": 0xFF55, "pagedown": 0xFF56, "capslock": 0xFFE5,
    "shift": 0xFFE1, "shiftleft": 0xFFE1, "shiftright": 0xFFE2,
    "ctrl": 0xFFE3, "ctrlleft": 0xFFE3, "ctrlright": 0xFFE4,
    "alt": 0xFFE9, "altleft": 0xFFE9, "altright": 0xFFEA,
    "win": 0xFFEB, "winleft": 0xFFEB, "winright": 0xFFEC,
    "space": 0x20,
    **{f"f{n}": 0xFFBD + n for n in range(1, 25)},
    **{chr(n): n for n in range(32, 127)},
}


class PortalInput:
    """The small synchronous input interface used by NativeDesktop's worker."""

    KEYBOARD_KEYS = KEYSYMS.keys()

    def __init__(self, desktop):
        self.desktop = desktop
        self.position = None
        self.moved = 0.0
        self.held = set()

    def notify(self, member, signature, *body):
        self.desktop.sync(self.desktop.notify(member, signature, *body))

    def absolute(self, x, y):
        self.notify("NotifyPointerMotionAbsolute", "udd", self.desktop.node, float(x), float(y))

    def moveTo(self, x, y):
        scale = self.desktop.scale
        if scale == 1:
            return self.absolute(x, y)
        # On a scaled display GNOME takes absolute positions in stream pixels, but the
        # portal rejects any beyond the smaller logical size, so most of the screen is
        # out of reach. Relative motion is logical and has no such limit.
        position, self.position = self.position, None
        now = time.monotonic()
        if position is None or (not self.held and now - self.moved > 1):
            # Someone may have moved the mouse since; start from a known place.
            near_x = min(x, self.desktop.screen_width / scale - 1)
            near_y = min(y, self.desktop.screen_height / scale - 1)
            self.absolute(near_x, near_y)
            position = near_x / scale, near_y / scale
        target = x / scale, y / scale
        if target != position:
            self.notify("NotifyPointerMotion", "dd", target[0] - position[0], target[1] - position[1])
        self.position, self.moved = target, now

    # Coordinates match pyautogui's signatures; moveTo has already applied them.
    def mouseDown(self, x=None, y=None, *, button):
        self.held.add(button)
        self.notify("NotifyPointerButton", "iu", {"left": 272, "right": 273, "middle": 274}[button], 1)

    def mouseUp(self, x=None, y=None, *, button):
        self.held.discard(button)
        self.notify("NotifyPointerButton", "iu", {"left": 272, "right": 273, "middle": 274}[button], 0)

    def click(self, x=None, y=None, *, button, clicks=1, interval=0):
        for n in range(clicks):
            if n:
                time.sleep(interval)
            try:
                self.mouseDown(button=button)
            finally:
                self.mouseUp(button=button)

    def scroll(self, amount, x=None, y=None):
        self.notify("NotifyPointerAxisDiscrete", "ui", 0, -amount)

    def axis(self, x, y):
        self.notify("NotifyPointerAxis", "dd", float(x), float(y))

    def hscroll(self, amount, x=None, y=None):
        self.notify("NotifyPointerAxisDiscrete", "ui", 1, amount)

    def keyDown(self, key):
        self.notify("NotifyKeyboardKeysym", "iu", KEYSYMS[key], 1)

    def keyUp(self, key):
        self.notify("NotifyKeyboardKeysym", "iu", KEYSYMS[key], 0)

    def hotkey(self, *keys):
        pressed = []
        try:
            for key in keys:
                self.keyDown(key)
                pressed.append(key)
        finally:
            for key in reversed(pressed):
                self.keyUp(key)


class PortalDesktop(NativeDesktop):
    fps = 60  # Frames arrive from PipeWire, so an unchanged one costs nothing.
    scale = 1

    def __init__(self, fs):
        super().__init__(fs)
        self.bus = None
        self.session = None
        self.closed = False
        self.latest = None
        self.reader_task = None
        self.stderr_task = None
        self.capture_stderr = ""
        self.first_frame = asyncio.Event()
        self.pending = {}
        self.clipboard_enabled = False
        self.clipboard_data = b""
        self.clipboard_tasks = set()
        self.failure = "Desktop sharing has ended. Reconnect and approve sharing on the Linux desktop."

    def sync(self, coroutine, timeout=10):
        # Input is called on NativeDesktop's worker; D-Bus stays on the API loop.
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    async def call(self, interface, member, signature="", body=None, path=PATH,
                   destination=DESTINATION):
        try:
            reply = await asyncio.wait_for(self.bus.call(Message(
                destination=destination, path=path, interface=interface,
                member=member, signature=signature, body=body or [],
            )), 10)
        except TimeoutError as exc:
            raise RuntimeError(f"Desktop portal {interface}.{member} did not reply within 10 seconds. Check the desktop portal services on the logged-in user's session.") from exc
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"Desktop portal {member}: {reply.body[0] if reply.body else reply.error_name}")
        return reply

    def on_message(self, message):
        if message.message_type != MessageType.SIGNAL:
            return
        if message.interface == REQUEST and message.member == "Response":
            future = self.pending.get(message.path)
            if future is not None and not future.done():
                future.set_result(message.body)
        elif message.interface == CLIPBOARD and message.member == "SelectionTransfer" and message.body[0] == self.session:
            self.transfer_selection(message.body[2])
        elif message.interface == SESSION and message.member == "Closed" and message.path == self.session:
            self.closed = True
            self.latest = None
            self.first_frame.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("The desktop portal closed the sharing session."))

    async def request(self, interface, member, signature="", body=None, options=None):
        token = "open_terminal_" + secrets.token_hex(12)
        sender = self.bus.unique_name[1:].replace(".", "_")
        path = f"{PATH}/request/{sender}/{token}"
        future = asyncio.get_running_loop().create_future()
        self.pending[path] = future
        opts = {**(options or {}), "handle_token": Variant("s", token)}
        log.info("Desktop portal: %s.%s", interface, member)
        try:
            reply = await self.call(interface, member, signature + "a{sv}", [*(body or []), opts])
            # Portals may return a different request handle than the proposed one.
            returned_path = reply.body[0]
            if returned_path != path:
                self.pending.pop(path, None)
                path = returned_path
                self.pending[path] = future
            timeout = 120 if member == "Start" else 10
            try:
                response, results = await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                hint = (
                    "Unlock the Ubuntu desktop and approve its screen-sharing dialog; the dialog appears on the desktop, not in SSH."
                    if member == "Start" else
                    "Check xdg-desktop-portal and xdg-desktop-portal-gnome in the logged-in user's session."
                )
                raise RuntimeError(f"Desktop portal {member} timed out after {timeout} seconds waiting for its Response signal. {hint}") from exc
            if response == 1:
                raise HTTPException(403, "Desktop sharing was not approved. Approve screen and input access on the logged-in Linux desktop.")
            if response != 0:
                raise RuntimeError(f"Desktop portal {member} failed. Check that the desktop supports remote control and is unlocked.")
            return results
        except BaseException:
            with contextlib.suppress(Exception):
                await self.call(REQUEST, "Close", path=path)
            raise
        finally:
            self.pending.pop(path, None)

    async def pointer_scale(self, position):
        """GNOME reports a scaled monitor's logical size, but reads absolute
        pointer positions in its stream's pixels. Other portals use logical."""
        try:
            _, _, monitors, properties = (await self.call(
                DISPLAY_CONFIG, "GetCurrentState",
                path="/org/gnome/Mutter/DisplayConfig", destination=DISPLAY_CONFIG,
            )).body
        except RuntimeError as exc:
            log.info("No GNOME display scale; using logical pointer positions: %s", exc)
            return 1
        layout = properties.get("layout-mode")
        if position is None or (layout and layout.value == 2):
            return 1
        return next((scale for x, y, scale, *_ in monitors if [x, y] == list(position.value)), 1)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        stage = "connecting to the desktop user's D-Bus session"
        try:
            # SSH may lack DISPLAY and DBUS_SESSION_BUS_ADDRESS. Connect only to
            # this OS user's existing bus, never launch a graphical/login session.
            address, capture_env = host_session()
            self.env.update(capture_env)
            self.bus = MessageBus(bus_address=address, negotiate_unix_fd=True)
            await asyncio.wait_for(self.bus.connect(), 10)
            self.bus.add_message_handler(self.on_message)
            stage = "subscribing to desktop portal responses"
            for interface in (REQUEST, SESSION, CLIPBOARD):
                reply = await asyncio.wait_for(self.bus.call(Message(
                    destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                    body=[f"type='signal',sender='{DESTINATION}',interface='{interface}'"],
                )), 10)
                if reply.message_type == MessageType.ERROR:
                    raise RuntimeError("Could not listen for desktop portal permission changes")
            version = (await self.call(
                "org.freedesktop.DBus.Properties", "Get", "ss", [REMOTE, "version"],
            )).body[0].value
            devices = {"types": Variant("u", 3)}
            saved_token = None
            if version >= 2:
                devices["persist_mode"] = Variant("u", 2)
                saved_token = read_restore_token()
                if saved_token:
                    devices["restore_token"] = Variant("s", saved_token)
            else:
                log.warning("This desktop portal cannot remember authorization; approve sharing for each new session.")
            stage = "creating the desktop sharing session"
            results = await self.request(REMOTE, "CreateSession", options={
                "session_handle_token": Variant("s", "open_terminal_" + secrets.token_hex(12)),
            })
            self.session = results["session_handle"].value
            await self.request(REMOTE, "SelectDevices", "o", [self.session], devices)
            await self.request(SCREENCAST, "SelectSources", "o", [self.session], {
                "types": Variant("u", 1), "multiple": Variant("b", False),
                # The viewer shows its own cursor; a second one would trail behind it.
                "cursor_mode": Variant("u", 1),
            })
            # Restore tokens are single-use, including attempts that time out.
            if saved_token:
                save_restore_token(None)
            # Optional, and a saved approval keeps whatever was granted with it.
            with contextlib.suppress(RuntimeError):
                await self.call(CLIPBOARD, "RequestClipboard", "oa{sv}", [self.session, {}])
            results = await self.request(REMOTE, "Start", "os", [self.session, ""])
            self.clipboard_enabled = results.get("clipboard_enabled", Variant("b", False)).value
            if version >= 2:
                replacement = results.get("restore_token")
                save_restore_token(replacement.value if replacement else None)
                if not replacement:
                    log.warning("The desktop portal did not grant persistent authorization; the next session will need approval.")
            streams = results.get("streams")
            if not streams or len(streams.value) != 1 or results.get("devices", Variant("u", 0)).value & 3 != 3:
                raise HTTPException(403, "Desktop sharing requires one monitor plus keyboard and pointer access.")
            self.node, properties = streams.value[0]
            size = properties.get("logical_size") or properties.get("size")
            if size is None or len(size.value) != 2 or any(n <= 0 or n > 32767 for n in size.value):
                raise RuntimeError("Desktop portal did not provide a valid monitor size")
            self.screen_width, self.screen_height = size.value
            scale = min(1, 1920 / self.screen_width, 1080 / self.screen_height)
            self.width, self.height = round(self.screen_width * scale), round(self.screen_height * scale)
            self.scale = await self.pointer_scale(properties.get("position"))
            self.screen_width, self.screen_height = self.screen_width * self.scale, self.screen_height * self.scale
            # Viewers get the display's own pixels; assistants keep the smaller picture.
            scale = min(1, 1920 / self.screen_width, 1080 / self.screen_height)
            self.frame = (round(self.screen_width * scale), round(self.screen_height * scale))
            self.input = PortalInput(self)
            reply = await self.call(SCREENCAST, "OpenPipeWireRemote", "oa{sv}", [self.session, {}])
            try:
                fd = reply.unix_fds[reply.body[0]]
                stage = "starting PipeWire screen capture"
                await self.start_capture(fd=fd, env=capture_env)
                await self.audio.start()
            finally:
                for fd in reply.unix_fds:
                    os.close(fd)
        except HTTPException:
            raise
        except Exception as exc:
            detail = str(exc).strip() or (f"Timed out while {stage}" if isinstance(exc, TimeoutError) else f"{type(exc).__name__} while {stage}")
            log.warning("Could not share the Linux desktop: %s", detail)
            raise HTTPException(503, f"Could not share the Linux desktop: {detail}") from exc

    async def start_capture(self, *, fd=None, env=None):
        width, height = self.frame_size
        process = await asyncio.create_subprocess_exec(
            "gst-launch-1.0", "-q", "pipewiresrc", f"path={self.node}",
            *((f"fd={fd}",) if fd is not None else ()),
            "!", "queue", "max-size-buffers=1", "leaky=downstream",
            "!", "videoconvert", "!", "videoscale",
            "!", f"video/x-raw,format=RGBx,width={width},height={height},pixel-aspect-ratio=1/1",
            "!", "fdsink", "fd=1", "sync=false",
            pass_fds=() if fd is None else (fd,), env=sanitized_environ(env),
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        self.stderr_task = asyncio.create_task(self.read_stderr(process))
        self.reader_task = asyncio.create_task(self.read_frames(process))
        try:
            await asyncio.wait_for(self.first_frame.wait(), 15)
        except TimeoutError as exc:
            raise RuntimeError("PipeWire capture produced no screen frame within 15 seconds." + self.capture_error()) from exc
        self.grab()

    async def read_stderr(self, process):
        while data := await process.stderr.read(4096):
            self.capture_stderr = (self.capture_stderr + data.decode(errors="replace"))[-4096:]

    def capture_error(self):
        return f" GStreamer: {self.capture_stderr.strip()}" if self.capture_stderr.strip() else ""

    async def read_frames(self, process):
        width, height = self.frame_size
        try:
            while True:
                data = await process.stdout.readexactly(width * height * 4)
                self.update_frame(data)
        except asyncio.IncompleteReadError:
            if self.stderr_task:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self.stderr_task), 1)
            self.failure = "Desktop capture stopped before another complete frame arrived. Check gstreamer1.0-pipewire and gstreamer1.0-plugins-base." + self.capture_error()
        finally:
            self.closed = True
            self.latest = None
            self.first_frame.set()

    def update_frame(self, data):
        self.latest = Image.frombytes("RGB", self.frame_size, data, "raw", "RGBX")
        self.first_frame.set()

    def grab(self):
        if self.closed or self.latest is None:
            raise RuntimeError(self.failure)
        return self.latest

    async def notify(self, member, signature, *body):
        if self.closed:
            raise RuntimeError(self.failure)
        await self.call(REMOTE, member, "oa{sv}" + signature, [self.session, {}, *body])

    def scroll(self, x, y):
        # Absolute positions use the stream's pixels, but scrolling is logical.
        self.input.axis(x / self.scale, y / self.scale)

    async def selection(self, member, signature, *body):
        return await self.call(CLIPBOARD, member, "o" + signature, [self.session, *body])

    def transfer_selection(self, serial):
        task = asyncio.create_task(self.write_selection(serial, self.clipboard_data))
        self.clipboard_tasks.add(task)
        task.add_done_callback(self.clipboard_tasks.discard)

    async def write_selection(self, serial, data):
        reply = None
        success = False
        try:
            reply = await self.selection("SelectionWrite", "u", serial)
            fd = reply.unix_fds[reply.body[0]]
            os.set_blocking(fd, False)
            remaining = memoryview(data)
            while remaining:
                try:
                    remaining = remaining[os.write(fd, remaining):]
                except BlockingIOError:
                    ready = self.loop.create_future()
                    self.loop.add_writer(fd, lambda: ready.done() or ready.set_result(None))
                    try:
                        await asyncio.wait_for(ready, 5)
                    finally:
                        self.loop.remove_writer(fd)
            success = True
        except (OSError, RuntimeError, TimeoutError):
            pass
        finally:
            if reply:
                for fd in reply.unix_fds:
                    os.close(fd)
            with contextlib.suppress(Exception):
                await self.selection("SelectionWriteDone", "ub", serial, success)

    def paste(self, text, shift=False):
        # Typing works in every app, but only for characters the keyboard layout has.
        if self.clipboard_enabled and not text.isascii():
            return self.paste_clipboard(text, shift)

        async def type_text():
            for char in text:
                key = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09}.get(char)
                if key is None:
                    key = ord(char) if ord(char) < 256 else 0x01000000 | ord(char)
                try:
                    await self.notify("NotifyKeyboardKeysym", "iu", key, 1)
                finally:
                    await self.notify("NotifyKeyboardKeysym", "iu", key, 0)

        self.sync(type_text(), timeout=max(10, len(text) / 100))

    def paste_clipboard(self, text, shift=False):
        keys = [0xFFE3, *([0xFFE1] if shift else []), ord("v")]

        async def transfer():
            self.clipboard_data = text.encode("utf-8")
            await self.selection("SetSelection", "a{sv}", {
                "mime_types": Variant("as", ["text/plain;charset=utf-8", "text/plain"]),
            })
            try:
                for key in keys:
                    await self.notify("NotifyKeyboardKeysym", "iu", key, 1)
            finally:
                for key in reversed(keys):
                    await self.notify("NotifyKeyboardKeysym", "iu", key, 0)

        self.sync(transfer())

    async def stop_session(self):
        if self.bus and self.session:
            await self.call(SESSION, "Close", path=self.session)

    async def close(self):
        self.closed = True
        self.latest = None
        try:
            await super().close()
        finally:
            for task in list(self.clipboard_tasks):
                task.cancel()
            await asyncio.gather(*self.clipboard_tasks, return_exceptions=True)
            if self.reader_task:
                self.reader_task.cancel()
                await asyncio.gather(self.reader_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.stop_session()
            if self.bus:
                self.bus.disconnect()
                with contextlib.suppress(Exception):
                    await self.bus.wait_for_disconnect()
            for process in reversed(self.processes):
                await self.stop_process(process)
            if self.stderr_task:
                self.stderr_task.cancel()
                await asyncio.gather(self.stderr_task, return_exceptions=True)

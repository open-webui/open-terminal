"""Virtual desktop manager — Xvfb + x11vnc + noVNC + xdotool.

Provides a programmatic "Computer Use" capability: the agent can see the
screen (screenshots) and interact with it (mouse clicks, keyboard input)
while a human observes in real-time via the embedded noVNC web client.
"""

import asyncio
import base64
import io
import logging
import os
import shutil
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)


class DesktopNotRunningError(RuntimeError):
    pass


class DesktopManager:
    """Manages the lifecycle of a virtual desktop environment.

    Components started (all as subprocesses of the current process):

    1. **Xvfb** — headless X server on a virtual display.
    2. **Window manager** — ``openbox`` (or whatever is available) so windows
       are usable.
    3. **x11vnc** — VNC server exposing the virtual display.
    4. **websockify + noVNC** — WebSocket proxy + embedded web VNC client so
       users can view the desktop in a browser.
    """

    def __init__(
        self,
        display: str = ':0',
        screen: str = '1280x720x24',
        vnc_port: int = 5900,
        novnc_port: int = 6080,
    ):
        self._display = display
        self._screen = screen
        self._vnc_port = vnc_port
        self._novnc_port = novnc_port
        self._xvfb: subprocess.Popen | None = None
        self._x11vnc: subprocess.Popen | None = None
        self._novnc: subprocess.Popen | None = None
        self._wm: subprocess.Popen | None = None
        self._lock = asyncio.Lock()
        self._cursor_overlay = None
        self._cursor_xdisplay = None

    @property
    def display(self) -> str:
        return self._display

    @property
    def vnc_port(self) -> int:
        return self._vnc_port

    @property
    def novnc_port(self) -> int:
        return self._novnc_port

    def _env(self) -> dict:
        env = os.environ.copy()
        env['DISPLAY'] = self._display
        return env

    def start(self) -> None:
        """Start all desktop components.

        Safe to call multiple times — subsequent calls are no-ops when the
        desktop is already running.
        """
        if self.is_running:
            return

        env = self._env()

        self._xvfb = subprocess.Popen(
            [
                'Xvfb',
                self._display,
                '-screen',
                '0',
                self._screen,
                '-ac',
                '+extension',
                'GLX',
                '-nolisten',
                'tcp',
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if self._xvfb.poll() is not None:
            raise RuntimeError('Xvfb failed to start')

        os.environ['DISPLAY'] = self._display

        if os.path.isdir('/usr/share/icons/DMZ-White'):
            subprocess.run(
                ['xsetroot', '-cursor_name', 'left_ptr'],
                env=env,
                capture_output=True,
                timeout=5,
            )

        wm = shutil.which('openbox') or shutil.which('xfwm4') or shutil.which('metacity')
        if wm:
            self._wm = subprocess.Popen(
                [wm],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)

        self._x11vnc = subprocess.Popen(
            [
                'x11vnc',
                '-display',
                self._display,
                '-rfbport',
                str(self._vnc_port),
                '-nopw',
                '-listen',
                '127.0.0.1',
                '-shared',
                '-forever',
                '-noxdamage',
                '-dontdisconnect',
                '-cursor',
                'X',
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)

        novnc_path = '/usr/share/novnc'
        if os.path.isdir(novnc_path):
            self._novnc = subprocess.Popen(
                [
                    'websockify',
                    '--web',
                    novnc_path,
                    str(self._novnc_port),
                    f'localhost:{self._vnc_port}',
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        logger.info(
            'Desktop started: display=%s vnc=%d novnc=%d',
            self._display,
            self._vnc_port,
            self._novnc_port,
        )

    async def async_start(self) -> None:
        """Async wrapper around :meth:`start`."""
        async with self._lock:
            await asyncio.to_thread(self.start)

    def stop(self) -> None:
        """Terminate all desktop subprocesses."""
        self._destroy_cursor_overlay()
        for proc in (self._novnc, self._x11vnc, self._wm, self._xvfb):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        self._novnc = None
        self._x11vnc = None
        self._wm = None
        self._xvfb = None
        os.environ.pop('DISPLAY', None)

    async def async_stop(self) -> None:
        """Async wrapper around :meth:`stop`."""
        async with self._lock:
            await asyncio.to_thread(self.stop)

    @property
    def is_running(self) -> bool:
        return self._xvfb is not None and self._xvfb.poll() is None

    def _require_running(self) -> None:
        if not self.is_running:
            raise DesktopNotRunningError('Desktop is not running. Start it with POST /desktop/start.')

    def get_screen_size(self) -> tuple[int, int]:
        """Return ``(width, height)`` of the virtual display."""
        self._require_running()
        result = subprocess.run(
            ['xdotool', 'getdisplaygeometry'],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        parts = result.stdout.strip().split()
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
        parts = self._screen.split('x')
        return int(parts[0]), int(parts[1])

    def get_mouse_location(self) -> tuple[int, int]:
        """Return ``(x, y)`` of the mouse in screen coordinates."""
        self._require_running()
        result = subprocess.run(
            ['xdotool', 'getmouselocation'],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        x = y = 0
        for part in result.stdout.strip().split():
            if part.startswith('x:'):
                x = int(part[2:])
            elif part.startswith('y:'):
                y = int(part[2:])
        return x, y

    def screenshot(self) -> tuple[bytes, int, int]:
        """Capture a PNG screenshot.

        Returns ``(png_bytes, width, height)``.
        """
        self._require_running()
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                [
                    'scrot',
                    '-o',
                    '-q',
                    '90',
                    tmp_path,
                ],
                env=self._env(),
                check=True,
                capture_output=True,
                timeout=10,
            )
            with open(tmp_path, 'rb') as f:
                data = f.read()
            w, h = self.get_screen_size()
            return data, w, h
        except FileNotFoundError:
            subprocess.run(
                ['import', '-window', 'root', '-quality', '90', tmp_path],
                env=self._env(),
                check=True,
                capture_output=True,
                timeout=10,
            )
            with open(tmp_path, 'rb') as f:
                data = f.read()
            w, h = self.get_screen_size()
            return data, w, h
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def async_screenshot(self) -> tuple[bytes, int, int]:
        return await asyncio.to_thread(self.screenshot)

    def annotated_screenshot(self) -> tuple[bytes, int, int]:
        png_bytes, real_w, real_h = self.screenshot()
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img)

        mx, my = self.get_mouse_location()
        draw.ellipse([mx - 5, my - 5, mx + 5, my + 5], fill='red')
        draw.ellipse([mx - 3, my - 3, mx + 3, my + 3], fill='white')

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        return buf.getvalue(), real_w, real_h

    async def async_annotated_screenshot(self) -> tuple[bytes, int, int]:
        return await asyncio.to_thread(self.annotated_screenshot)

    def mouse_move(self, x: int, y: int) -> None:
        self._require_running()
        subprocess.run(
            ['xdotool', 'mousemove', str(x), str(y)],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )
        self._update_cursor_overlay(x, y)

    def mouse_click(self, x: int, y: int, button: int = 1) -> None:
        self._require_running()
        self._hide_cursor_overlay()
        subprocess.run(
            [
                'xdotool',
                'mousemove',
                str(x),
                str(y),
                'click',
                str(button),
            ],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )
        self._update_cursor_overlay(x, y)
        time.sleep(0.3)

    def _update_cursor_overlay(self, x: int, y: int) -> None:
        from Xlib import X
        from Xlib.display import Display as XDisplay
        from Xlib.ext import shape

        try:
            if self._cursor_xdisplay is None:
                self._cursor_xdisplay = XDisplay(self._display)

            disp = self._cursor_xdisplay
            screen = disp.screen()

            size = 13
            half = size // 2
            win_w = size + 4
            win_h = size + 4
            cx = half + 2
            cy = half + 2

            if self._cursor_overlay is not None:
                try:
                    self._cursor_overlay.unmap()
                    self._cursor_overlay.destroy()
                except Exception:
                    pass

            overlay = screen.root.create_window(
                x - half - 2,
                y - half - 2,
                win_w,
                win_h,
                0,
                screen.root_depth,
                X.InputOutput,
                screen.root_visual,
                background_pixel=0,
                override_redirect=True,
                event_mask=0,
            )

            mask = overlay.create_pixmap(win_w, win_h, 1)
            gc0 = mask.create_gc(foreground=0)
            mask.fill_rectangle(gc0, 0, 0, win_w, win_h)
            gc1 = mask.create_gc(foreground=1)
            mask.line(gc1, cx - half, cy, cx + half, cy)
            mask.line(gc1, cx, cy - half, cx, cy + half)
            shape.mask(overlay, shape.SO.Set, shape.SK.Bounding, 0, 0, mask)

            white = screen.default_colormap.alloc_color(65535, 65535, 65535).pixel
            gc_w = overlay.create_gc(foreground=white, line_width=2)
            overlay.line(gc_w, cx - half, cy, cx + half, cy)
            overlay.line(gc_w, cx, cy - half, cx, cy + half)

            overlay.map()
            disp.flush()
            self._cursor_overlay = overlay
        except Exception:
            self._cursor_overlay = None
            self._cursor_xdisplay = None

    def _hide_cursor_overlay(self) -> None:
        if self._cursor_overlay is not None:
            try:
                self._cursor_overlay.unmap()
                if self._cursor_xdisplay is not None:
                    self._cursor_xdisplay.flush()
            except Exception:
                pass

    def _destroy_cursor_overlay(self) -> None:
        if self._cursor_overlay is not None:
            try:
                self._cursor_overlay.unmap()
                self._cursor_overlay.destroy()
                if self._cursor_xdisplay is not None:
                    self._cursor_xdisplay.flush()
            except Exception:
                pass
            self._cursor_overlay = None
        if self._cursor_xdisplay is not None:
            try:
                self._cursor_xdisplay.close()
            except Exception:
                pass
            self._cursor_xdisplay = None

    def mouse_down(self, x: int, y: int, button: int = 1) -> None:
        self._require_running()
        self._hide_cursor_overlay()
        subprocess.run(
            ['xdotool', 'mousemove', str(x), str(y)],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            ['xdotool', 'mousedown', str(button)],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )

    def mouse_up(self, x: int, y: int, button: int = 1) -> None:
        self._require_running()
        self._hide_cursor_overlay()
        subprocess.run(
            ['xdotool', 'mousemove', str(x), str(y)],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            ['xdotool', 'mouseup', str(button)],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )
        self._update_cursor_overlay(x, y)

    def mouse_drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: int = 1,
    ) -> None:
        self._require_running()
        env = self._env()
        self._hide_cursor_overlay()
        subprocess.run(
            ['xdotool', 'mousemove', str(start_x), str(start_y)],
            env=env,
            check=True,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            ['xdotool', 'mousedown', str(button)],
            env=env,
            check=True,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            [
                'xdotool',
                'mousemove',
                '--sync',
                str(end_x),
                str(end_y),
            ],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ['xdotool', 'mouseup', str(button)],
            env=env,
            check=True,
            capture_output=True,
            timeout=5,
        )
        self._update_cursor_overlay(end_x, end_y)

    def type_text(self, text: str, human_like: bool = True) -> None:
        self._require_running()
        env = self._env()
        if human_like:
            for char in text:
                delay = max(5, min(120, int(30 + 25 * ((ord(char) * 7 % 11) / 10.0))))
                subprocess.run(
                    ['xdotool', 'type', '--clearmodifiers', '--delay', str(delay), char],
                    env=env,
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
                time.sleep(delay / 1000.0 + 0.005 * (ord(char) % 5))
        else:
            subprocess.run(
                ['xdotool', 'type', '--clearmodifiers', '--delay', '12', text],
                env=env,
                check=True,
                capture_output=True,
                timeout=max(10, len(text) // 5),
            )

    def key_press(self, key: str) -> None:
        self._require_running()
        subprocess.run(
            ['xdotool', 'key', '--clearmodifiers', key],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )

    def scroll(self, x: int, y: int, direction: str, amount: int = 5) -> None:
        """Scroll at ``(x, y)``.  *direction* is ``"up"`` or ``"down"``."""
        self._require_running()
        env = self._env()
        self._hide_cursor_overlay()
        subprocess.run(
            ['xdotool', 'mousemove', str(x), str(y)],
            env=env,
            check=True,
            capture_output=True,
            timeout=5,
        )
        button = 5 if direction == 'down' else 4
        for _ in range(amount):
            subprocess.run(
                ['xdotool', 'click', str(button)],
                env=env,
                check=True,
                capture_output=True,
                timeout=5,
            )
            time.sleep(0.02)
        self._update_cursor_overlay(x, y)

    def list_windows(self) -> list[dict]:
        self._require_running()
        result = subprocess.run(
            ['xdotool', 'search', '--onlyvisible', '--name', ''],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        window_ids = [w.strip() for w in result.stdout.strip().splitlines() if w.strip()]
        if not window_ids:
            return []

        cmd = ['xprop', '-root', '_NET_CLIENT_LIST']
        root_result = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=5)
        client_windows = set()
        for line in root_result.stdout.splitlines():
            for part in line.split():
                try:
                    client_windows.add(str(int(part, 16)))
                except (ValueError, TypeError):
                    pass
                try:
                    if part.startswith('0x'):
                        client_windows.add(str(int(part, 16)))
                except (ValueError, TypeError):
                    pass

        windows = []
        for wid in window_ids:
            try:
                name_result = subprocess.run(
                    ['xdotool', 'getwindowname', wid],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                name = name_result.stdout.strip()

                geom_result = subprocess.run(
                    ['xdotool', 'getwindowgeometry', '--shell', wid],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                geom = {}
                for line in geom_result.stdout.strip().splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        geom[k] = int(v)

                pid_result = subprocess.run(
                    ['xdotool', 'getwindowpid', wid],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                pid = pid_result.stdout.strip()

                active_result = subprocess.run(
                    ['xdotool', 'getactivewindow'],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                active = active_result.stdout.strip() == wid

                windows.append({
                    'id': wid,
                    'name': name or '(untitled)',
                    'x': geom.get('X', 0),
                    'y': geom.get('Y', 0),
                    'width': geom.get('WIDTH', 0),
                    'height': geom.get('HEIGHT', 0),
                    'pid': pid if pid.isdigit() else None,
                    'active': active,
                })
            except Exception:
                continue

        return windows

    async def async_list_windows(self) -> list[dict]:
        return await asyncio.to_thread(self.list_windows)

    def focus_window(self, window_id: str) -> None:
        self._require_running()
        subprocess.run(
            ['xdotool', 'windowactivate', '--sync', window_id],
            env=self._env(),
            check=True,
            capture_output=True,
            timeout=5,
        )

    async def async_focus_window(self, window_id: str) -> None:
        await asyncio.to_thread(self.focus_window, window_id)

    def status(self) -> dict:
        """Return a dict describing the current desktop state."""
        running = self.is_running
        info: dict = {
            'running': running,
            'display': self._display,
            'vnc_port': self._vnc_port,
            'novnc_port': self._novnc_port,
        }
        if running:
            w, h = self.get_screen_size()
            info['screen_width'] = w
            info['screen_height'] = h
        return info


_desktop: DesktopManager | None = None


def get_desktop() -> DesktopManager:
    """Return (and lazily create) the global :class:`DesktopManager`."""
    global _desktop
    if _desktop is None:
        from open_terminal.env import (
            DESKTOP_DISPLAY,
            DESKTOP_NOVNC_PORT,
            DESKTOP_SCREEN_SIZE,
            DESKTOP_VNC_PORT,
        )

        _desktop = DesktopManager(
            display=DESKTOP_DISPLAY,
            screen=DESKTOP_SCREEN_SIZE,
            vnc_port=DESKTOP_VNC_PORT,
            novnc_port=DESKTOP_NOVNC_PORT,
        )
    return _desktop

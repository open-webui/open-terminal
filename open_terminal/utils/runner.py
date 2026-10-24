import asyncio
import json
import os
import shlex
import signal
import subprocess
import time
from abc import ABC, abstractmethod

from open_terminal.env import sanitized_environ

try:
    import fcntl
    import pty
    import struct
    import termios

    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False  # Windows

try:
    from winpty import PtyProcess as WinPtyProcess

    _WINPTY_AVAILABLE = True
except ImportError:
    _WINPTY_AVAILABLE = False


async def read_pty(fd: int) -> bytes:
    """Wait for descriptor readiness without occupying a worker thread."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            return os.read(fd, 4096)
        except BlockingIOError:
            ready = loop.create_future()
            def readable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(fd, readable)
            try:
                await ready
            finally:
                loop.remove_reader(fd)
        except OSError:
            return b""


async def write_pty(fd: int, data: bytes):
    loop = asyncio.get_running_loop()
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        except BlockingIOError:
            ready = loop.create_future()
            def writable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_writer(fd, writable)
            try:
                await ready
            finally:
                loop.remove_writer(fd)


class ProcessRunner(ABC):
    """Unified interface for running a subprocess via PTY or pipes."""

    @abstractmethod
    async def read_output(self, log_file) -> None:
        """Read output from the process and write entries to *log_file*."""

    @abstractmethod
    def write_input(self, data: bytes) -> None:
        """Send *data* to the process's stdin / PTY."""

    @abstractmethod
    def kill(self, force: bool = False) -> None:
        """Terminate (SIGTERM) or kill (SIGKILL) the process."""

    @abstractmethod
    async def wait(self) -> int:
        """Wait for the process to exit and return the exit code."""

    @abstractmethod
    def close(self) -> None:
        """Release file descriptors and other resources."""

    @property
    @abstractmethod
    def pid(self) -> int:
        """PID of the child process."""


class PtyRunner(ProcessRunner):
    """Spawn a command under a pseudo-terminal (Unix)."""

    def __init__(self, command: str, cwd: str | None, env: dict | None, run_as_user: str | None = None):
        if run_as_user:
            # Build the inner command: optionally cd first, then run the command.
            inner = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
            # ``sudo`` resets the environment by default.  ``-E`` preserves
            # it so that caller-supplied env vars (e.g. ``GH_TOKEN``) actually
            # reach the child process.
            command = f"sudo -E -u {shlex.quote(run_as_user)} -- bash -c {shlex.quote(inner)}"
            cwd = None  # Popen runs as parent user — can't chdir into chmod 700 dirs
        master_fd, slave_fd = pty.openpty()
        try:
            # Set a reasonable default window size (80x24).
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            self._process = subprocess.Popen(
                command,
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=sanitized_environ(env),
                start_new_session=True,
            )
        except Exception:
            os.close(slave_fd)
            os.close(master_fd)
            raise
        os.close(slave_fd)
        self._master_fd = master_fd
        os.set_blocking(master_fd, False)

    async def read_output(self, log_file) -> None:
        while True:
            try:
                data = await read_pty(self._master_fd)
                if not data:
                    break
            except OSError:
                break  # EIO when child exits
            if log_file:
                await log_file.write(
                    json.dumps(
                        {
                            "type": "output",
                            "data": data.decode(errors="replace"),
                            "ts": time.time(),
                        }
                    )
                    + "\n"
                )

    def write_input(self, data: bytes) -> None:
        os.write(self._master_fd, data)

    def _signal_group(self, sig: int) -> None:
        """Send *sig* to the child's entire process group.

        Falls back to signalling just the leader if the group is already gone.
        """
        try:
            os.killpg(self._process.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self._process.send_signal(sig)
            except ProcessLookupError:
                pass

    def kill(self, force: bool = False) -> None:
        self._signal_group(signal.SIGKILL if force else signal.SIGTERM)

    async def wait(self) -> int:
        while self._process.poll() is None:
            await asyncio.sleep(0.05)
        return self._process.returncode

    def close(self) -> None:
        if self._master_fd is not None:
            fd, self._master_fd = self._master_fd, None
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def pid(self) -> int:
        return self._process.pid


class PipeRunner(ProcessRunner):
    """Spawn a command with stdin/stdout/stderr pipes (cross-platform fallback)."""

    def __init__(self, command: str, cwd: str | None, env: dict | None):
        self._process: asyncio.subprocess.Process = None  # type: ignore[assignment]
        self._command = command
        self._cwd = cwd
        self._env = sanitized_environ(env)

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_shell(
            self._command,
            start_new_session=os.name != "nt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )

    async def read_output(self, log_file) -> None:
        async def read_stream(stream, label):
            async for line in stream:
                if log_file:
                    await log_file.write(
                        json.dumps(
                            {
                                "type": label,
                                "data": line.decode(errors="replace"),
                                "ts": time.time(),
                            }
                        )
                        + "\n"
                    )

        async with asyncio.TaskGroup() as readers:
            readers.create_task(read_stream(self._process.stdout, "stdout"))
            readers.create_task(read_stream(self._process.stderr, "stderr"))

    def write_input(self, data: bytes) -> None:
        self._process.stdin.write(data)

    async def drain_input(self) -> None:
        await self._process.stdin.drain()

    def kill(self, force: bool = False) -> None:
        if os.name == "nt":
            if self._process.returncode is None:
                self._process.kill() if force else self._process.terminate()
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(self._process.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            # No dedicated process group — fall back to the child directly.
            try:
                self._process.send_signal(sig)
            except ProcessLookupError:
                pass

    async def wait(self) -> int:
        # After a timeout cancels the readers, paused pipe transports can keep
        # wait() pending even after the child is killed. Drain before reaping.
        async def discard(stream):
            while await stream.read(4096):
                pass
        await asyncio.gather(discard(self._process.stdout), discard(self._process.stderr))
        await self._process.wait()
        return self._process.returncode

    def close(self) -> None:
        if self._process.stdin:
            self._process.stdin.close()

    @property
    def pid(self) -> int:
        return self._process.pid


class WinPtyRunner(ProcessRunner):
    """Spawn a command under a Windows pseudo-terminal (ConPTY via pywinpty)."""

    def __init__(self, command: str, cwd: str | None, env: dict | None):
        spawn_env = sanitized_environ(env)

        # Determine the executable and arguments.
        # PtyProcess.spawn expects a list: [executable, *args]
        shell = spawn_env.get("COMSPEC", "cmd.exe")
        cmd_args = [shell, "/c", command] if command else [shell]

        self._pty = WinPtyProcess.spawn(
            cmd_args,
            cwd=cwd,
            env=spawn_env,
            dimensions=(24, 80),
        )

    async def read_output(self, log_file) -> None:
        loop = asyncio.get_event_loop()

        def _read_blocking():
            try:
                return self._pty.read(4096)
            except EOFError:
                return ""
            except Exception:
                return ""

        while True:
            data = await loop.run_in_executor(None, _read_blocking)
            if not data:
                if not self._pty.isalive():
                    break
                await asyncio.sleep(0.05)
                continue
            if log_file:
                await log_file.write(
                    json.dumps(
                        {
                            "type": "output",
                            "data": data,
                            "ts": time.time(),
                        }
                    )
                    + "\n"
                )

    def write_input(self, data: bytes) -> None:
        self._pty.write(data.decode(errors="replace"))

    def kill(self, force: bool = False) -> None:
        if force:
            self._pty.kill(signal.SIGKILL)
        else:
            self._pty.terminate()

    async def wait(self) -> int:
        while self._pty.isalive():
            await asyncio.sleep(0.1)
        return self._pty.exitstatus or 0

    def close(self) -> None:
        if self._pty.isalive():
            self._pty.terminate()

    @property
    def pid(self) -> int:
        return self._pty.pid

    def set_size(self, rows: int, cols: int) -> None:
        """Resize the pseudo-terminal window."""
        self._pty.setwinsize(rows, cols)


async def create_runner(
    command: str,
    cwd: str | None,
    env: dict | None,
    run_as_user: str | None = None,
) -> ProcessRunner:
    """Factory: create a PTY runner on Unix, WinPTY runner on Windows, or pipe fallback."""
    if _PTY_AVAILABLE:
        return PtyRunner(command, cwd, env, run_as_user=run_as_user)
    if _WINPTY_AVAILABLE:
        return WinPtyRunner(command, cwd, env)
    runner = PipeRunner(command, cwd, env)
    await runner.start()
    return runner

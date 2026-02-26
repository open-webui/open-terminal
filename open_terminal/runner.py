import asyncio
import json
import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod

try:
    import fcntl
    import pty
    import struct
    import termios

    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False  # Windows


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

    def __init__(self, command: str, cwd: str | None, env: dict | None):
        master_fd, slave_fd = pty.openpty()
        # Set a reasonable default window size (80x24).
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self._process = subprocess.Popen(
            command,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd

    async def read_output(self, log_file) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, self._master_fd, 4096)
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
                await log_file.flush()

    def write_input(self, data: bytes) -> None:
        os.write(self._master_fd, data)

    def kill(self, force: bool = False) -> None:
        if force:
            self._process.kill()
        else:
            self._process.send_signal(signal.SIGTERM)

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)

    def close(self) -> None:
        try:
            os.close(self._master_fd)
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
        self._env = env

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_shell(
            self._command,
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
                    await log_file.flush()

        await asyncio.gather(
            read_stream(self._process.stdout, "stdout"),
            read_stream(self._process.stderr, "stderr"),
        )

    def write_input(self, data: bytes) -> None:
        self._process.stdin.write(data)

    async def drain_input(self) -> None:
        await self._process.stdin.drain()

    def kill(self, force: bool = False) -> None:
        if force:
            self._process.kill()
        else:
            self._process.send_signal(signal.SIGTERM)

    async def wait(self) -> int:
        await self._process.wait()
        return self._process.returncode

    def close(self) -> None:
        pass  # pipes are cleaned up automatically

    @property
    def pid(self) -> int:
        return self._process.pid


async def create_runner(command: str, cwd: str | None, env: dict | None) -> ProcessRunner:
    """Factory: create a PTY runner on Unix, pipe runner on Windows."""
    if _PTY_AVAILABLE:
        return PtyRunner(command, cwd, env)
    runner = PipeRunner(command, cwd, env)
    await runner.start()
    return runner

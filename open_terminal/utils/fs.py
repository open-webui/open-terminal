"""Filesystem abstraction for multi-user mode.

Provides :class:`UserFS`, a unified interface for file operations.

**Reads** always use native Python I/O.  In multi-user mode the server
process is added to each provisioned user's group (via ``usermod -aG``)
and home directories are ``chmod 750``, so standard ``open()``,
``os.listdir()``, ``os.stat()`` etc. work without subprocess.

**Writes** route through ``sudo -u`` when a username is set to ensure
correct file ownership.  In single-user mode they fall back to
stdlib / aiofiles.
"""

import asyncio
import os
import shutil
import subprocess

import aiofiles
import aiofiles.os


def _sudo_cmd(username: str) -> list[str]:
    """Base sudo prefix for running a command as *username*."""
    return ["sudo", "-u", username, "--"]


class UserFS:
    """Filesystem operations scoped to an optional OS user.

    *username* controls write-side ``sudo -u`` wrapping (``None`` = stdlib).
    *home* is the user's home directory (default working directory).

    When *username* is set, path validation prevents access to other
    users' home directories (``/home/<other_user>/…``).
    """

    def __init__(self, username: str | None = None, home: str | None = None):
        self.username = username
        self.home = home or os.getcwd()

    def _check_path(self, path: str) -> None:
        """Reject paths inside another user's home directory."""
        if not self.username:
            return
        resolved = os.path.abspath(path)
        # Only restrict paths under /home/
        if not resolved.startswith("/home/"):
            return
        # Extract the first component after /home/
        parts = resolved.split("/")  # ['', 'home', '<user>', ...]
        if len(parts) >= 3:
            target_user_dir = parts[2]
            own_home_name = os.path.basename(self.home)
            if target_user_dir != own_home_name:
                raise PermissionError(
                    f"Access denied: {resolved} belongs to another user"
                )

    # ------------------------------------------------------------------
    # Read operations (always native Python — group membership allows it)
    # ------------------------------------------------------------------

    async def read(self, path: str) -> bytes:
        """Read raw bytes from *path*."""
        self._check_path(path)
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read text from *path*."""
        self._check_path(path)
        async with aiofiles.open(path, "r", encoding=encoding, errors="strict") as f:
            return await f.read()

    async def exists(self, path: str) -> bool:
        """Check if *path* exists."""
        self._check_path(path)
        return await aiofiles.os.path.exists(path)

    async def isfile(self, path: str) -> bool:
        """Check if *path* is a regular file."""
        self._check_path(path)
        return await aiofiles.os.path.isfile(path)

    async def isdir(self, path: str) -> bool:
        """Check if *path* is a directory."""
        self._check_path(path)
        return await aiofiles.os.path.isdir(path)

    async def stat(self, path: str) -> dict:
        """Return size, mtime, and type for *path*."""
        self._check_path(path)
        s = await aiofiles.os.stat(path)
        return {
            "size": s.st_size,
            "modified": s.st_mtime,
            "type": "directory" if os.path.isdir(path) else "file",
        }

    async def listdir(self, path: str) -> list[dict]:
        """List directory contents with type, size, and mtime."""
        self._check_path(path)
        def _list_sync():
            entries = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                try:
                    s = os.stat(full)
                    entries.append({
                        "name": name,
                        "type": "directory" if os.path.isdir(full) else "file",
                        "size": s.st_size,
                        "modified": s.st_mtime,
                    })
                except OSError:
                    continue
            return entries
        return await asyncio.to_thread(_list_sync)

    async def walk(self, path: str) -> list[tuple[str, list[str], list[str]]]:
        """Walk directory tree. Returns list of (dirpath, dirnames, filenames)."""
        self._check_path(path)
        return await asyncio.to_thread(lambda: list(os.walk(path)))

    # ------------------------------------------------------------------
    # Write operations (sudo -u when username is set for correct ownership)
    # ------------------------------------------------------------------

    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """Write text *content* to *path*, creating parent dirs."""
        self._check_path(path)
        if self.username:
            parent = os.path.dirname(path)
            if parent:
                await asyncio.to_thread(
                    subprocess.run,
                    _sudo_cmd(self.username) + ["mkdir", "-p", parent],
                    check=True, capture_output=True,
                )
            await asyncio.to_thread(
                subprocess.run,
                _sudo_cmd(self.username) + ["tee", path],
                input=content.encode(encoding),
                check=True, capture_output=True,
            )
            return
        parent = os.path.dirname(path)
        if parent:
            await aiofiles.os.makedirs(parent, exist_ok=True)
        async with aiofiles.open(path, "w", encoding=encoding) as f:
            await f.write(content)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write raw *data* to *path*, creating parent dirs."""
        self._check_path(path)
        if self.username:
            parent = os.path.dirname(path)
            if parent:
                await asyncio.to_thread(
                    subprocess.run,
                    _sudo_cmd(self.username) + ["mkdir", "-p", parent],
                    check=True, capture_output=True,
                )
            await asyncio.to_thread(
                subprocess.run,
                _sudo_cmd(self.username) + ["tee", path],
                input=data,
                check=True, capture_output=True,
            )
            return
        parent = os.path.dirname(path)
        if parent:
            await aiofiles.os.makedirs(parent, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def mkdir(self, path: str) -> None:
        """Create directory *path* and parents."""
        self._check_path(path)
        if self.username:
            await asyncio.to_thread(
                subprocess.run,
                _sudo_cmd(self.username) + ["mkdir", "-p", path],
                check=True, capture_output=True,
            )
            return
        await aiofiles.os.makedirs(path, exist_ok=True)

    async def remove(self, path: str) -> None:
        """Remove *path* (file or directory)."""
        self._check_path(path)
        if self.username:
            await asyncio.to_thread(
                subprocess.run,
                _sudo_cmd(self.username) + ["rm", "-rf", path],
                check=True, capture_output=True,
            )
            return
        if os.path.isdir(path):
            await asyncio.to_thread(shutil.rmtree, path)
        else:
            await aiofiles.os.remove(path)

    async def move(self, source: str, destination: str) -> None:
        """Move *source* to *destination*."""
        self._check_path(source)
        self._check_path(destination)
        if self.username:
            await asyncio.to_thread(
                subprocess.run,
                _sudo_cmd(self.username) + ["mv", source, destination],
                check=True, capture_output=True,
            )
            return
        await asyncio.to_thread(shutil.move, source, destination)

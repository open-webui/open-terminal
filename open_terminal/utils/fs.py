"""Filesystem abstraction for multi-user mode.

Provides :class:`UserFS`, a unified interface for file operations.

All I/O uses native Python (``aiofiles`` / ``os``).  In multi-user mode
the server process is added to each provisioned user's group, and home
directories are ``chmod 2770`` (setgid + group rwx), so standard file
operations work without subprocess.

After each write operation a ``sudo chown`` call fixes file ownership
so that files belong to the provisioned user, not the server process.
"""

import asyncio
import os
import io
import json
import re
import tempfile
import shutil
import subprocess

import aiofiles
import aiofiles.os
from starlette.responses import FileResponse

from open_terminal.utils.limits import CHUNK_SIZE, run_io


class FullFileResponse(FileResponse):
    """Stream the original full response without adding HTTP range/cache semantics."""

    def set_stat_headers(self, stat_result):
        self.headers.setdefault("content-length", str(stat_result.st_size))

    async def __call__(self, scope, receive, send):
        if "accept-ranges" in self.headers:
            del self.headers["accept-ranges"]
        scope = {**scope, "headers": [(key, value) for key, value in scope.get("headers", []) if key.lower() not in (b"range", b"if-range")]}
        await super().__call__(scope, receive, send)


class TemporaryFileResponse(FullFileResponse):
    """Remove generated files even when sending the response is cancelled."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


_LINE_BREAK = re.compile(r"\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def line_range_offsets(text, start_line=None, end_line=None):
    start_boundary = (start_line or 1) - 1
    start = 0 if start_boundary == 0 else len(text)
    end = len(text)
    last_boundary = max(start_boundary, end_line or 0)
    if last_boundary:
        for number, match in enumerate(_LINE_BREAK.finditer(text), 1):
            if number == start_boundary:
                start = match.end()
            if number == end_line:
                end = match.end()
            if number >= last_boundary:
                break
    return start, end


def text_chunks(source):
    """Yield splitlines-compatible line numbers and bounded pieces of text."""
    number = 1
    pending = ""
    for chunk in iter(lambda: source.read(CHUNK_SIZE), ""):
        chunk = pending + chunk
        pending = "\r" if chunk.endswith("\r") else ""
        if pending:
            chunk = chunk[:-1]
        start = 0
        for match in _LINE_BREAK.finditer(chunk):
            end = match.end()
            yield number, chunk[start:end]
            number += 1
            start = end
        if start < len(chunk):
            yield number, chunk[start:]
    if pending:
        yield number, pending


def read_text_range(path, start_line=None, end_line=None):
    selected = io.StringIO()
    total = 0
    with open(path, encoding="utf-8") as source:
        for total, chunk in text_chunks(source):
            if total >= (start_line or 1) and (end_line is None or total <= end_line):
                selected.write(chunk)
    return {"total_lines": total, "content": selected.getvalue()}


def _build_text_file_response(path, display_path, start_line=None, end_line=None, normalize_newlines=True):
    """Build the original JSON response on disk, including arbitrarily long lines."""
    fd, temporary = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output, open(path, encoding="utf-8", newline=None if normalize_newlines else "") as source:
            output.write('{"path":' + json.dumps(display_path) + ',"content":"')
            total = 0
            for total, chunk in text_chunks(source):
                if total >= (start_line or 1) and (end_line is None or total <= end_line):
                    output.write(json.dumps(chunk, ensure_ascii=False)[1:-1])
            output.write('","total_lines":' + str(total) + '}')
        return TemporaryFileResponse(temporary, media_type="application/json")
    except BaseException:
        os.unlink(temporary)
        raise


async def text_file_response(*args):
    task = asyncio.create_task(asyncio.to_thread(_build_text_file_response, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            response = await task
        except Exception:
            pass
        else:
            os.unlink(response.path)
        raise


class UserFS:
    """Filesystem operations scoped to an optional OS user.

    *username* is used for ownership fixups after writes (``None`` = stdlib).
    *home* is the user's home directory (default working directory).

    When *username* is set, path validation prevents access to other
    users' home directories (``/home/<other_user>/…``).
    """

    def __init__(self, username: str | None = None, home: str | None = None):
        self.username = username
        self.home = home or os.getcwd()

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve_path(self, path: str, cwd: str | None = None) -> str:
        """Resolve *path* to an absolute path relative to the user's home.

        Absolute paths are normalised in place.  Relative paths are joined
        to *cwd* (if provided) or ``self.home`` so that they resolve against
        the session's working directory rather than the server process's
        ``os.getcwd()``.

        In multi-user mode, paths under ``/home/user`` (the server process's
        default home) are automatically rewritten to the provisioned user's
        home directory, since LLMs often hardcode that path.
        """
        if os.path.isabs(path):
            # Swap /home/user (and /home/usr, a common LLM hallucination)
            # → user's actual home when multi-user is active
            if self.username and self.home != "/home/user":
                for prefix in ("/home/user", "/home/usr"):
                    if path == prefix:
                        path = self.home
                        break
                    elif path.startswith(prefix + "/"):
                        path = self.home + path[len(prefix):]
                        break
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(cwd or self.home, path))

    # ------------------------------------------------------------------
    # Path validation
    # ------------------------------------------------------------------

    def is_path_allowed(self, path: str) -> bool:
        """Return *False* if *path* is inside another user's home directory."""
        if not self.username:
            return True
        resolved = os.path.abspath(path)
        if not resolved.startswith("/home/"):
            return True
        parts = resolved.split("/")  # ['', 'home', '<user>', ...]
        if len(parts) >= 3:
            target_user_dir = parts[2]
            own_home_name = os.path.basename(self.home)
            if target_user_dir != own_home_name:
                return False
        return True

    def _check_path(self, path: str) -> None:
        """Reject paths inside another user's home directory."""
        if not self.is_path_allowed(path):
            raise PermissionError(
                f"Access denied: {os.path.abspath(path)} belongs to another user"
            )

    async def _chown(self, path: str) -> None:
        """Fix ownership of *path* to the provisioned user.

        Also sets group-write permission so the server process (which is
        in the provisioned user's group) can overwrite the file on
        subsequent writes.
        """
        if self.username:
            await asyncio.to_thread(
                subprocess.run,
                ["sudo", "chown", f"{self.username}:{self.username}", path],
                check=True, capture_output=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["sudo", "chmod", "g+w", path],
                check=True, capture_output=True,
            )

    async def _ensure_parents(self, path: str) -> None:
        """Create parent directories for *path* with correct permissions.

        In multi-user mode, uses ``sudo -u`` to create directories as the
        provisioned user (so creation succeeds even inside ``755`` dirs
        made by ``run_command``), then sets ``2770`` on each directory in
        the chain so the server process has group-write access.

        In single-user mode, falls back to plain ``makedirs``.
        """
        if not self.username:
            await aiofiles.os.makedirs(path, exist_ok=True)
            return
        # Create as the provisioned user to bypass 755 restrictions.
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", self.username, "mkdir", "-p", path],
            check=True, capture_output=True,
        )
        # Walk upward, setting 2770 so the server process (which is in the
        # user's group) can create files inside these directories.
        target = os.path.normpath(path)
        home = os.path.normpath(self.home)
        while target != home and target.startswith(home + "/"):
            await asyncio.to_thread(
                subprocess.run,
                ["sudo", "chmod", "2770", target],
                check=True, capture_output=True,
            )
            target = os.path.dirname(target)

    # ------------------------------------------------------------------
    # Read operations
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
            "writable": self._is_writable_sync(path),
        }

    def _is_writable_sync(self, path: str) -> bool:
        if not self.is_path_allowed(path):
            return False
        try:
            if os.statvfs(path).f_flag & getattr(os, "ST_RDONLY", 0):
                return False
        except (AttributeError, OSError):
            pass
        try:
            return os.access(path, os.W_OK, effective_ids=True)
        except (TypeError, OSError, NotImplementedError, AttributeError):
            return os.access(path, os.W_OK)

    async def is_writable(self, path: str) -> bool:
        """Return whether the current process can write to *path*."""
        return await asyncio.to_thread(self._is_writable_sync, path)

    async def listdir(self, path: str) -> list[dict]:
        """List contents with type, size, modification/creation times, and writability."""
        self._check_path(path)
        def _list_sync():
            entries = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if not self.is_path_allowed(full):
                    continue
                try:
                    s = os.stat(full)
                    entries.append({
                        "name": name,
                        "type": "directory" if os.path.isdir(full) else "file",
                        "size": s.st_size,
                        "modified": s.st_mtime,
                        "created": getattr(s, "st_birthtime", s.st_ctime if os.name == "nt" else None),
                        "writable": self._is_writable_sync(full),
                    })
                except OSError:
                    continue
            # Python's Unix stat lacks birth time on Linux; ask GNU stat in batches.
            if entries and entries[0]["created"] is None:
                for offset in range(0, len(entries), 256):
                    batch = entries[offset:offset + 256]
                    try:
                        result = subprocess.run(
                            ["stat", "-L", r"--printf=%n\0%.9W\0", "--", *[e["name"] for e in batch]],
                            cwd=path, capture_output=True, timeout=10,
                            env={**os.environ, "LC_ALL": "C"},
                        )
                        if result.returncode and not result.stdout:
                            break
                        fields = result.stdout.split(b"\0")
                        created = dict(zip(fields[::2], fields[1::2]))
                        for entry in batch:
                            entry["created"] = float(created.get(os.fsencode(entry["name"]), b"0")) or None
                    except (OSError, subprocess.TimeoutExpired, ValueError):
                        break
            return entries
        return await run_io(_list_sync)

    async def walk(self, path: str) -> list[tuple[str, list[str], list[str]]]:
        """Walk directory tree. Returns list of (dirpath, dirnames, filenames).

        In multi-user mode, directories belonging to other users are pruned
        so their contents are never yielded.
        """
        self._check_path(path)
        def _walk_filtered():
            result = []
            for dirpath, dirnames, filenames in os.walk(path):
                # Prune directories belonging to other users (in-place
                # modification prevents os.walk from descending into them).
                dirnames[:] = [
                    d for d in dirnames
                    if self.is_path_allowed(os.path.join(dirpath, d))
                ]
                filenames = [
                    f for f in filenames
                    if self.is_path_allowed(os.path.join(dirpath, f))
                ]
                result.append((dirpath, dirnames, filenames))
            return result
        return await run_io(_walk_filtered)

    # ------------------------------------------------------------------
    # Write operations (native Python + chown for correct ownership)
    # ------------------------------------------------------------------

    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """Write text *content* to *path*, creating parent dirs."""
        self._check_path(path)
        parent = os.path.dirname(path)
        if parent:
            await self._ensure_parents(parent)
        async with aiofiles.open(path, "w", encoding=encoding) as f:
            await f.write(content)
        await self._chown(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write raw *data* to *path*, creating parent dirs."""
        self._check_path(path)
        parent = os.path.dirname(path)
        if parent:
            await self._ensure_parents(parent)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        await self._chown(path)

    async def mkdir(self, path: str) -> None:
        """Create directory *path* and parents."""
        self._check_path(path)
        await self._ensure_parents(path)

    async def remove(self, path: str) -> None:
        """Remove *path* (file or directory)."""
        self._check_path(path)
        if os.path.isdir(path):
            await asyncio.to_thread(shutil.rmtree, path)
        else:
            await aiofiles.os.remove(path)

    async def move(self, source: str, destination: str) -> None:
        """Move *source* to *destination*."""
        self._check_path(source)
        self._check_path(destination)
        await asyncio.to_thread(shutil.move, source, destination)
        await self._chown(destination)

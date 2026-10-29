"""Incremental filesystem searches with disk-backed Git and ripgrep results."""
from contextlib import contextmanager
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from open_terminal.utils.limits import CHUNK_SIZE


_PSEUDO_FILESYSTEMS = ("/dev", "/proc", "/sys")


def is_regular_file(path):
    """True only for regular files; reading devices, FIFOs or sockets can block forever."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _device(path):
    try:
        return os.lstat(path).st_dev
    except OSError:
        return None


def _is_pseudo_filesystem(path):
    """Avoid handing pseudo-filesystem trees (notably /dev) to ripgrep."""
    path = os.path.realpath(path)
    return any(path == mount or path.startswith(mount + os.sep) for mount in _PSEUDO_FILESYSTEMS)


def walk_entries(fs, root, show_hidden=True):
    # Stay on the root's filesystem so pseudo-filesystems such as /dev, /proc and /sys are skipped.
    root_device = _device(root)
    for directory, dirs, files in os.walk(root):
        dirs[:] = [name for name in sorted(dirs)
                   if (show_hidden or not name.startswith("."))
                   and fs.is_path_allowed(os.path.join(directory, name))
                   and _device(os.path.join(directory, name)) in (root_device, None)]
        for name, kind in sorted([(name, "directory") for name in dirs] + [(name, "file") for name in files]):
            path = os.path.join(directory, name)
            if (show_hidden or not name.startswith(".")) and fs.is_path_allowed(path):
                yield path, kind


def _nul_records(source):
    pending = b""
    while chunk := source.read(CHUNK_SIZE):
        records = (pending + chunk).split(b"\0")
        pending = records.pop()
        yield from records
    if pending:
        yield pending


@contextmanager
def disk_database():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(os.path.join(directory, "search.sqlite"))
        try:
            db.execute("PRAGMA cache_size = -1024")
            db.execute("PRAGMA temp_store = FILE")
            yield db
        finally:
            db.close()


def search_entries(fs, root, show_hidden=False, *, git_files_as_files=False):
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                ["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z", "--", "."],
                stdout=output, stderr=subprocess.DEVNULL,
            )
        except OSError:
            result = None
        if result is None or result.returncode:
            yield from walk_entries(fs, root, show_hidden)
            return
        output.seek(0)
        with disk_database() as db:
            db.execute("CREATE TABLE entries (path BLOB PRIMARY KEY)")
            for raw in _nul_records(output):
                rel = os.fsdecode(raw)
                if not show_hidden and any(part.startswith(".") for part in rel.replace("\\", "/").split("/")):
                    continue
                path = os.path.normpath(os.path.join(root, rel))
                if not fs.is_path_allowed(path):
                    continue
                leaf = path
                while path != root and path.startswith(root.rstrip(os.sep) + os.sep):
                    if fs.is_path_allowed(path):
                        cursor = db.execute("INSERT OR IGNORE INTO entries VALUES (?)", (os.fsencode(path),))
                        if cursor.rowcount:
                            kind = "file" if git_files_as_files and path == leaf else "directory" if os.path.isdir(path) else "file"
                            yield path, kind
                    path = os.path.dirname(path)


@contextmanager
def matching_files(query, root, show_hidden):
    """Use rg's ignore rules without its potentially enormous JSON submatch arrays."""
    # --one-file-system protects a search rooted at / from crossing into /dev on typical
    # Linux hosts, but it cannot help when /dev itself is the requested root. In that case
    # use the guarded Python fallback, which opens only regular files.
    if _is_pseudo_filesystem(root):
        yield None
        return
    rg = shutil.which("rg")
    if not rg:
        yield None
        return
    with tempfile.TemporaryFile() as output:
        args = [rg, "--files-with-matches", "--null", "--no-messages", "--fixed-strings", "--ignore-case", "--one-file-system"]
        if os.path.realpath(root) == os.path.sep:
            # Also exclude these mount points if a container happens to report the same st_dev.
            for name in ("dev", "proc", "sys"):
                args.extend(["--glob", f"!{name}/**"])
        if show_hidden:
            args.append("--hidden")
        try:
            result = subprocess.run([*args, "--", query, root], stdout=output, stderr=subprocess.DEVNULL)
        except OSError:
            result = None
        if result is None or result.returncode not in (0, 1):
            yield None
            return
        output.seek(0)
        with disk_database() as db:
            db.execute("CREATE TABLE matches (path BLOB PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO matches VALUES (?)",
                           ((os.fsencode(os.path.normpath(os.fsdecode(raw))),) for raw in _nul_records(output)))
            yield lambda path: db.execute("SELECT 1 FROM matches WHERE path = ?", (os.fsencode(path),)).fetchone() is not None


def ranked_page(candidates, offset, limit):
    # Deep pagination must not keep offset + limit full-content matches in RAM.
    with disk_database() as db:
        db.execute("CREATE TABLE results (score INTEGER, length INTEGER, name TEXT, payload TEXT)")
        total = 0
        for score, length, name, result in candidates:
            db.execute("INSERT INTO results VALUES (?, ?, ?, ?)", (score, length, name, json.dumps(result)))
            total += 1
        rows = db.execute("SELECT payload FROM results ORDER BY score, length, name, rowid LIMIT ? OFFSET ?", (limit, offset))
        return {"results": [json.loads(row[0]) for row in rows],
                "next_offset": offset + limit if total > offset + limit else None}

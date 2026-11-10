"""Start directory and setup script.

``OPEN_TERMINAL_START_DIR`` is where every session, shell, and command begins
unless a request names a directory. It is resolved inside the home folder,
which is the only place a managed terminal keeps files across restarts.

``OPEN_TERMINAL_SETUP_SCRIPT`` runs in that directory each time the server
starts, before it accepts requests. Its result is kept under ``~/.open-terminal``
so system info can report a failed setup.
"""

import json
import os
import shutil
import subprocess
import time
from contextlib import suppress

from open_terminal.env import SETUP_SCRIPT, START_DIR, sanitized_environ

STATE_DIR = ".open-terminal"
LOG_TAIL_BYTES = 4096


def _start_target(home: str) -> str:
    """The configured start directory inside *home*, or *home* when unset or outside it."""
    value = START_DIR
    if not value or value == "~":
        return home
    if value.startswith("~/"):
        value = value[2:]
    # os.path.join keeps an absolute value as-is; the check below confines it to home.
    path = os.path.realpath(os.path.join(home, value))
    root = os.path.realpath(home)
    if path != root and not path.startswith(root + os.sep):
        return home
    return path


def start_directory(home: str) -> str:
    """Directory new sessions start in. Falls back to *home* until the folder exists."""
    path = _start_target(home)
    return path if os.path.isdir(path) else home


def _status_path(home: str) -> str:
    return os.path.join(home, STATE_DIR, "setup.json")


def _log_path(home: str) -> str:
    return os.path.join(home, STATE_DIR, "setup.log")


def _write_status(home: str, status: dict) -> None:
    path = _status_path(home)
    with open(f"{path}.tmp", "w") as handle:
        json.dump(status, handle)
    os.replace(f"{path}.tmp", path)


def run_setup(home: str) -> dict | None:
    """Create the start directory, then run the setup script and record its result.

    A failing script never stops the server: the machine still starts and system
    info reports the failure with the end of the log.
    """
    target = _start_target(home)
    with suppress(OSError):
        os.makedirs(target, exist_ok=True)
    if not SETUP_SCRIPT.strip():
        with suppress(FileNotFoundError):
            os.remove(_status_path(home))
        return None

    os.makedirs(os.path.join(home, STATE_DIR), exist_ok=True)
    bash = shutil.which("bash")
    # -e stops at the first failing command; bash -l also loads ~/.profile (PATH, tool managers).
    command = [bash, "-l", "-e", "-c", SETUP_SCRIPT] if bash else ["/bin/sh", "-e", "-c", SETUP_SCRIPT]
    started = time.time()
    _write_status(home, {"status": "running", "started_at": started})
    with open(_log_path(home), "wb") as log:
        try:
            code = subprocess.run(
                command,
                cwd=start_directory(home),
                env=sanitized_environ({"HOME": home}),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            ).returncode
        except OSError as exc:
            log.write(f"{exc}\n".encode())
            code = 127
    status = {
        "status": "ok" if code == 0 else "failed",
        "exit_code": code,
        "started_at": started,
        "finished_at": time.time(),
    }
    _write_status(home, status)
    return status


def setup_status(home: str) -> dict | None:
    """The last setup result, with the end of the log when it failed."""
    try:
        with open(_status_path(home)) as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return None
    if status.get("status") == "failed":
        try:
            with open(_log_path(home), "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
                status["log_tail"] = handle.read().decode(errors="replace")
        except OSError:
            pass
    return status

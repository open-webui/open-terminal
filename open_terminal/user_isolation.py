"""Per-user OS account provisioning for multi-user mode.

When ``OPEN_TERMINAL_MULTI_USER=true``, each distinct ``X-User-Id`` is mapped
to a dedicated Linux user account.  Commands and file operations then run as
that OS user via ``sudo -u``, and ``chmod 700`` on the home directory provides
kernel-enforced isolation between users.
"""

import hashlib
import logging
import platform
import pwd
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

# In-memory cache: upstream user-id → (os_username, home_dir)
_user_cache: dict[str, tuple[str, str]] = {}


def check_environment() -> None:
    """Validate that the host supports multi-user mode.

    Raises ``RuntimeError`` at startup when the platform is not Linux or
    ``sudo`` is not available.
    """
    if platform.system() != "Linux":
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires Linux "
            f"(current platform: {platform.system()})"
        )
    if shutil.which("sudo") is None:
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires sudo to be installed"
        )
    if shutil.which("useradd") is None:
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires useradd to be installed"
        )


def sanitize_username(user_id: str) -> str:
    """Convert an arbitrary user ID into a valid Linux username.

    Uses the first 8 lowercase alphanumeric characters of the user ID,
    optionally prefixed by ``OPEN_TERMINAL_USER_PREFIX``.  Prepends ``u``
    only when the result starts with a digit (Linux usernames must begin
    with a letter or underscore).  Falls back to a short hash when the ID
    contains fewer than 4 usable characters.
    """
    from open_terminal.env import USER_PREFIX

    cleaned = re.sub(r"[^a-z0-9]", "", user_id.lower())
    if len(cleaned) >= 4:
        name = cleaned[:8]
    else:
        # Fallback: hash-based name for very short / non-alphanumeric IDs
        name = hashlib.sha256(user_id.encode()).hexdigest()[:8]
    name = f"{USER_PREFIX}{name}"
    # Linux usernames must start with a letter or underscore
    if name[0].isdigit():
        name = f"u{name}"
    return name


def ensure_os_user(username: str) -> str:
    """Create the OS user if it doesn't exist (idempotent).

    Sets ``chmod 700`` on the home directory so other users cannot read it.
    Returns the home directory path.
    """
    try:
        pw = pwd.getpwnam(username)
        return pw.pw_dir
    except KeyError:
        pass  # User doesn't exist yet — create below

    log.info("Provisioning OS user: %s", username)
    subprocess.run(
        ["sudo", "useradd", "-m", "-s", "/bin/bash", username],
        check=True,
        capture_output=True,
    )
    home_dir = f"/home/{username}"
    subprocess.run(
        ["sudo", "chmod", "700", home_dir],
        check=True,
        capture_output=True,
    )
    return home_dir


def resolve_user(user_id: str) -> tuple[str, str]:
    """Map an upstream user ID to an OS user, provisioning if needed.

    Returns ``(username, home_dir)``.  Results are cached in-memory so
    repeated requests for the same user skip the syscall / subprocess.
    """
    cached = _user_cache.get(user_id)
    if cached is not None:
        return cached

    username = sanitize_username(user_id)
    home_dir = ensure_os_user(username)
    _user_cache[user_id] = (username, home_dir)
    return username, home_dir


# ---------------------------------------------------------------------------
# Sudo-wrapped file helpers (async-friendly via asyncio.to_thread)
# ---------------------------------------------------------------------------

import asyncio
import shlex


def _sudo_cmd(username: str) -> list[str]:
    """Base sudo prefix for running a command as *username*."""
    return ["sudo", "-u", username, "--"]


async def sudo_write_file(username: str, path: str, content: str) -> None:
    """Write *content* to *path* as *username*, creating parent dirs."""
    async def _write():
        subprocess.run(
            _sudo_cmd(username) + ["mkdir", "-p", shlex.quote(os.path.dirname(path))],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            _sudo_cmd(username) + ["tee", path],
            input=content.encode(),
            check=True, capture_output=True,
        )
    await asyncio.to_thread(_write)


async def sudo_mkdir(username: str, path: str) -> None:
    """Create directory *path* (and parents) as *username*."""
    await asyncio.to_thread(
        subprocess.run,
        _sudo_cmd(username) + ["mkdir", "-p", path],
        check=True, capture_output=True,
    )


async def sudo_rm(username: str, path: str) -> None:
    """Remove *path* (file or directory) as *username*."""
    await asyncio.to_thread(
        subprocess.run,
        _sudo_cmd(username) + ["rm", "-rf", path],
        check=True, capture_output=True,
    )


async def sudo_mv(username: str, source: str, destination: str) -> None:
    """Move *source* to *destination* as *username*."""
    await asyncio.to_thread(
        subprocess.run,
        _sudo_cmd(username) + ["mv", source, destination],
        check=True, capture_output=True,
    )


async def sudo_list_dir(username: str, path: str) -> list[dict]:
    """List directory contents as *username*."""
    script = (
        f'for f in $(ls -A {shlex.quote(path)} 2>/dev/null); do '
        f'  full="{path}/$f"; '
        f'  if [ -d "$full" ]; then t=directory; else t=file; fi; '
        f'  s=$(stat -c %s "$full" 2>/dev/null || echo 0); '
        f'  m=$(stat -c %Y "$full" 2>/dev/null || echo 0); '
        f'  echo "$f|$t|$s|$m"; '
        f'done'
    )
    result = await asyncio.to_thread(
        subprocess.run,
        _sudo_cmd(username) + ["bash", "-c", script],
        capture_output=True, text=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            entries.append({
                "name": parts[0],
                "type": parts[1],
                "size": int(parts[2]),
                "modified": float(parts[3]),
            })
    return sorted(entries, key=lambda e: e["name"])


async def sudo_read_file(username: str, path: str) -> bytes:
    """Read file contents as *username*. Returns raw bytes."""
    result = await asyncio.to_thread(
        subprocess.run,
        _sudo_cmd(username) + ["cat", path],
        capture_output=True,
    )
    if result.returncode != 0:
        raise PermissionError(result.stderr.decode().strip())
    return result.stdout


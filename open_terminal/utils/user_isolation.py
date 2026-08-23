"""Per-user OS account provisioning for multi-user mode.

When ``OPEN_TERMINAL_MULTI_USER=true``, each distinct ``X-User-Id`` is mapped
to a dedicated Linux user account.  Commands and file operations then run as
that OS user via ``sudo -u``, and ``chmod 700`` on the home directory provides
kernel-enforced isolation between users.
"""

import hashlib
import logging
import os
import platform
import pwd
import re
import shutil
import subprocess

from open_terminal.env import USER_PREFIX

log = logging.getLogger(__name__)

# In-memory cache: upstream user-id → (os_username, home_dir)
_user_cache: dict[str, tuple[str, str]] = {}

# useradd creates accounts at this UID or above on every mainstream distro
# (Debian/Ubuntu/RHEL default UID_MIN is 1000). Anything below it is a
# pre-existing system/privileged account (root, daemon, www-data, ...) that
# must never be reachable through the per-tenant sudo -u isolation path.
_MIN_PROVISIONED_UID = 1000


def _is_system_account(username: str) -> bool:
    """True if *username* already exists as a system or server-owned account.

    A sanitized ``X-User-Id`` can legitimately collide with the name of a
    real system account (e.g. a header value of ``root`` sanitizes to the
    literal username ``root``) or with the server process's own account
    (e.g. ``user`` in the default Docker image, which has passwordless sudo).
    Since every OS-level operation in multi-user mode is performed via
    ``sudo -u <username>``, resolving to either would hand the caller that
    account's privileges instead of a sandboxed per-tenant one.
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False
    if pw.pw_uid < _MIN_PROVISIONED_UID:
        return True
    return pw.pw_uid == os.getuid()


def _run_privileged(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command with appropriate privilege escalation.

    When the process is already running as root (UID 0), the command is
    executed directly.  Otherwise ``sudo`` is prepended.
    """
    if os.getuid() == 0:
        return subprocess.run(cmd, check=True, capture_output=True)
    return subprocess.run(["sudo", *cmd], check=True, capture_output=True)


def check_environment() -> None:
    """Validate that the host supports multi-user mode.

    Raises ``RuntimeError`` at startup when the platform is not Linux or
    the required privilege escalation tools are not available.
    """
    if platform.system() != "Linux":
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires Linux "
            f"(current platform: {platform.system()})"
        )
    if shutil.which("useradd") is None:
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires useradd to be installed"
        )
    if os.getuid() != 0 and shutil.which("sudo") is None:
        raise RuntimeError(
            "OPEN_TERMINAL_MULTI_USER requires either running as root "
            "or sudo to be installed. Use the standard image, run with "
            "user: '0:0', or use Terminals for container-per-user isolation."
        )


def sanitize_username(user_id: str) -> str:
    """Convert an arbitrary user ID into a valid Linux username.

    Uses the first 8 lowercase alphanumeric characters of the user ID,
    optionally prefixed by ``OPEN_TERMINAL_USER_PREFIX``.  Prepends ``u``
    only when the result starts with a digit (Linux usernames must begin
    with a letter or underscore).  Falls back to a short hash when the ID
    contains fewer than 4 usable characters.
    """
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

    Sets ``chmod 2770`` on the home directory and adds the server process
    user to the new user's primary group.  This allows native Python I/O
    for reads while other provisioned users still get ``Permission denied``.
    Returns the home directory path.
    """
    try:
        pw = pwd.getpwnam(username)
        return pw.pw_dir
    except KeyError:
        pass  # User doesn't exist yet — create below

    log.info("Provisioning OS user: %s", username)
    _run_privileged(["useradd", "-m", "-s", "/bin/bash", username])
    home_dir = f"/home/{username}"
    # Fix ownership (home dir may pre-exist from a previous run with a
    # different UID assignment) and set permissions.
    _run_privileged(["chown", "-R", f"{username}:{username}", home_dir])
    _run_privileged(["chmod", "2770", home_dir])
    # Add the server process user to the new user's group so Python can
    # read files natively without subprocess.
    server_user = os.getenv("USER", "user")
    _run_privileged(["usermod", "-aG", username, server_user])
    # If the Docker socket is mounted, add the new user to its group
    # so docker commands work without sudo (mirrors entrypoint.sh).
    _DOCKER_SOCK = "/var/run/docker.sock"
    if os.path.exists(_DOCKER_SOCK):
        import grp as _grp
        sock_gid = os.stat(_DOCKER_SOCK).st_gid
        try:
            sock_group = _grp.getgrgid(sock_gid).gr_name
            _run_privileged(["usermod", "-aG", sock_group, username])
            log.info("Added %s to Docker socket group '%s'", username, sock_group)
        except (KeyError, subprocess.CalledProcessError) as exc:
            log.warning("Could not add %s to Docker socket group: %s", username, exc)
    # Refresh the running process's supplementary group list so the new
    # group takes effect immediately (normally requires re-login).
    import ctypes
    import ctypes.util
    import grp

    pw = pwd.getpwnam(server_user)
    group_ids = sorted({
        g.gr_gid for g in grp.getgrall() if server_user in g.gr_mem
    } | {pw.pw_gid})
    try:
        os.setgroups(group_ids)
    except PermissionError:
        # Container user may lack CAP_SETGID.  The group will take effect
        # after the next process restart; log a warning and continue.
        log.warning(
            "Could not refresh supplementary groups (missing CAP_SETGID). "
            "Restart the server for group changes to take effect."
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
    if _is_system_account(username):
        # The sanitized name collides with a pre-existing system/privileged
        # account (e.g. X-User-Id: "root"). Force a distinct name derived
        # from the full hash so we never provision or sudo -u into it.
        username = f"{USER_PREFIX}h{hashlib.sha256(user_id.encode()).hexdigest()[:8]}"
        if _is_system_account(username):
            raise RuntimeError(
                f"Resolved username {username!r} for X-User-Id collides with "
                "an existing system account; refusing to provision"
            )
    home_dir = ensure_os_user(username)
    _user_cache[user_id] = (username, home_dir)
    return username, home_dir

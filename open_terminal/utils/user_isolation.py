"""Per-user OS account provisioning for multi-user mode.

When ``OPEN_TERMINAL_MULTI_USER=true``, each distinct ``X-User-Id`` is mapped
to a dedicated Linux account. Commands run as that user. The file API accesses
its workspace through Unix group permissions.
"""

import fcntl
import grp
import hashlib
import logging
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys

log = logging.getLogger(__name__)

# In-memory cache: upstream user-id → (os_username, home_dir)
_user_cache: dict[str, tuple[str, str]] = {}


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


def _allocate_uid(username: str, homes: dict[str, os.stat_result]) -> int:
    # useradd -U creates a private group. Reserve both namespaces, including
    # homes whose owners have not logged into this container yet.
    used = {g.gr_gid for g in grp.getgrall()}
    for pw in pwd.getpwall():
        used.update((pw.pw_uid, pw.pw_gid))
    used.update((os.geteuid(), os.getegid()))
    for name, info in homes.items():
        if name != username:
            used.update((info.st_uid, info.st_gid))
    persisted = homes.get(username)
    if persisted and 1000 <= persisted.st_uid <= 60000 and persisted.st_uid not in used:
        return persisted.st_uid
    for uid in range(1000, 60001):
        if uid not in used:
            return uid
    raise RuntimeError("No free workspace UID/GID")


def _workspace_gid(home: str, pw: pwd.struct_passwd, group: grp.struct_group) -> int:
    if os.geteuid() == 0:
        return pw.pw_gid
    server = pwd.getpwuid(os.geteuid())
    if pw.pw_gid not in os.getgroups():
        if server.pw_name not in group.gr_mem and server.pw_gid != pw.pw_gid:
            _run_privileged(["usermod", "-aG", group.gr_name, server.pw_name])
        try:
            os.setgroups(os.getgrouplist(server.pw_name, server.pw_gid))
        except PermissionError:
            log.warning("Cannot refresh supplementary groups for paths outside %s", home)

    # Local volumes with working supplementary groups keep their existing layout.
    # AUTH_SYS preserves the primary GID even when NFS replaces supplementary groups.
    if pw.pw_gid in os.getgroups():
        mount = home if os.path.lexists(home) else "/home"
        fs_type = subprocess.check_output(["stat", "-f", "-c", "%T", "--", mount], text=True).strip()
        if fs_type not in ("nfs", "nfs4"):
            return pw.pw_gid

    gid = os.getegid()
    members = {account.pw_name for account in pwd.getpwall() if account.pw_gid == gid}
    for entry in grp.getgrall():
        if entry.gr_gid == gid:
            members.update(entry.gr_mem)
    sock = "/var/run/docker.sock"
    if members - {server.pw_name, "root"} or (os.path.exists(sock) and os.stat(sock).st_gid == gid):
        raise RuntimeError(f"Workspace access to {home} requires a dedicated primary group for the server")
    return gid


def _migrate_children(home_fd, owner, uid, private_gid, gid):
    def fail(error):
        raise error

    # Leave the top-level directory for the caller to mark complete. fwalk
    # anchors traversal to directory FDs and does not follow directory symlinks.
    for _, dirs, files, parent in os.fwalk(".", dir_fd=home_fd, onerror=fail):
        for name in dirs + files:
            fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if info.st_dev != owner.st_dev:
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if info.st_uid not in (owner.st_uid, uid):
                    continue
                # The procfs handle names the pinned inode, including for a
                # symlink. Renames during a repair cannot redirect the chown.
                handle = f"/proc/self/fd/{fd}"
                group = gid if info.st_gid in (owner.st_gid, private_gid) else info.st_gid
                if (info.st_uid, info.st_gid) != (uid, group):
                    os.chown(handle, uid, group)
                if gid != private_gid and group == gid and stat.S_ISDIR(info.st_mode):
                    # Imported directories can lack setgid. Their shell-created
                    # children must inherit the group used by the NFS client.
                    mode = stat.S_IMODE(os.fstat(fd).st_mode)
                    if not mode & stat.S_ISGID:
                        os.chmod(handle, mode | stat.S_ISGID)
            finally:
                os.close(fd)


def _repair_home(home, uid, private_gid, gid):
    try:
        os.mkdir(home, 0o700)
    except FileExistsError:
        pass
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        owner = os.fstat(fd)
        if (owner.st_uid, owner.st_gid) != (uid, gid):
            _migrate_children(fd, owner, uid, private_gid, gid)
            # Commit the home ownership last so a failed traversal is retried.
            os.fchown(fd, uid, gid)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o2770:
            os.fchmod(fd, 0o2770)
    finally:
        os.close(fd)


def _repair_ownership(home: str, pw: pwd.struct_passwd, gid: int) -> None:
    try:
        info = os.lstat(home)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"Expected a workspace directory at {home}")
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (pw.pw_uid, gid, 0o2770):
            return

    _run_privileged([sys.executable, "-I", __file__, home, str(pw.pw_uid), str(pw.pw_gid), str(gid)])


def _prepare_home(username: str) -> str:
    if not username or username in (".", "..") or "/" in username:
        raise ValueError(f"Invalid workspace account name {username!r}")
    if username == "user":
        raise RuntimeError(f"Refusing to use reserved account {username!r}")
    home = f"/home/{username}"
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        with os.scandir("/home") as entries:
            homes = {
                entry.name: entry.stat(follow_symlinks=False)
                for entry in entries if entry.is_dir(follow_symlinks=False)
            }
        if os.path.lexists(home) and username not in homes:
            raise RuntimeError(f"Expected a workspace directory at {home}")
        uid = _allocate_uid(username, homes)
        log.info("Provisioning OS user %s with UID %s", username, uid)
        _run_privileged(["useradd", "-m", "-U", "-u", str(uid), "-s", "/bin/bash", "--", username])
        pw = pwd.getpwnam(username)

    if pw.pw_uid < 1000 or pw.pw_uid in (os.getuid(), os.geteuid()):
        raise RuntimeError(f"Refusing to use reserved account {username!r}")
    try:
        group = grp.getgrgid(pw.pw_gid)
    except KeyError:
        return pw.pw_dir
    if pw.pw_dir != home or pw.pw_gid < 1000 or group.gr_name != username:
        # Externally managed accounts keep their administrator-configured home.
        return pw.pw_dir
    _repair_ownership(home, pw, _workspace_gid(home, pw, group))

    # If the Docker socket is mounted, add the new user to its group
    # so docker commands work without sudo (mirrors entrypoint.sh).
    _DOCKER_SOCK = "/var/run/docker.sock"
    if os.path.exists(_DOCKER_SOCK):
        sock_gid = os.stat(_DOCKER_SOCK).st_gid
        try:
            sock_group = grp.getgrgid(sock_gid).gr_name
            if sock_gid in os.getgrouplist(username, pw.pw_gid):
                return home
            _run_privileged(["usermod", "-aG", sock_group, username])
            log.info("Added %s to Docker socket group '%s'", username, sock_group)
        except (KeyError, subprocess.CalledProcessError) as exc:
            log.warning("Could not add %s to Docker socket group: %s", username, exc)
    return home


def ensure_os_user(username: str) -> str:
    """Serialize ID allocation and repair across request threads and workers."""
    # Lock a stable local inode. useradd replaces /etc/passwd during updates.
    fd = os.open("/etc", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _prepare_home(username)
    finally:
        os.close(fd)


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


if __name__ == "__main__":
    _repair_home(sys.argv[1], *map(int, sys.argv[2:]))

"""Port detection and reverse-proxy utilities."""

import os
import platform
import asyncio

import anyio

from open_terminal.utils.limits import run_io


_subscribers: set[asyncio.Queue] = set()
_scanner: asyncio.Task | None = None
_snapshot: list[dict] | None = None


async def watch_listening_ports():
    """Share one OS scan across viewers; publish only changed snapshots."""
    global _scanner, _snapshot
    queue = asyncio.Queue(maxsize=1)
    _subscribers.add(queue)
    if _snapshot is not None:
        queue.put_nowait(_snapshot)

    async def scan():
        global _snapshot
        try:
            while True:
                ports = await run_io(detect_listening_ports)
                if ports != _snapshot:
                    _snapshot = ports
                    for subscriber in _subscribers:
                        if subscriber.full():
                            subscriber.get_nowait()
                        subscriber.put_nowait(ports)
                await asyncio.sleep(5)
        except Exception as exc:
            for subscriber in _subscribers:
                if subscriber.full():
                    subscriber.get_nowait()
                subscriber.put_nowait(exc)

    if _scanner is None:
        _scanner = asyncio.create_task(scan())
    try:
        while True:
            snapshot = await queue.get()
            if isinstance(snapshot, Exception):
                raise snapshot
            yield snapshot
    finally:
        _subscribers.discard(queue)
        if not _subscribers:
            scanner, _scanner = _scanner, None
            _snapshot = None
            with anyio.CancelScope(shield=True):
                scanner.cancel()
                await asyncio.gather(scanner, return_exceptions=True)


def detect_listening_ports() -> list[dict]:
    """Detect TCP ports listening on localhost.

    Strategy:
      - Linux: parse /proc/net/tcp and /proc/net/tcp6 (fast, no subprocess)
      - macOS / fallback: run ``lsof -iTCP -sTCP:LISTEN -nP``
      - Windows: run ``netstat -ano``

    Returns a sorted list of dicts with keys: port, pid, process.
    """
    ports: dict[int, dict] = {}  # port -> {port, pid, process}

    # --- Linux: /proc/net/tcp ---
    def _parse_proc_net_tcp():
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) < 8 or parts[3] != "0A":  # 0A = LISTEN
                            continue
                        local_addr = parts[1]
                        port = int(local_addr.split(":")[1], 16)
                        if port == 0:
                            continue
                        uid = int(parts[7]) if len(parts) > 7 else None
                        inode = parts[9] if len(parts) > 9 else ""
                        pid = _pid_from_inode(inode) if inode else None
                        pname = _process_name(pid) if pid else None
                        if port not in ports:
                            ports[port] = {
                                "port": port,
                                "pid": pid,
                                "process": pname,
                                "uid": uid,
                            }
            except FileNotFoundError:
                continue

    def _pid_from_inode(inode: str) -> int | None:
        """Resolve a socket inode to a PID by scanning /proc/*/fd/."""
        try:
            target = f"socket:[{inode}]"
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                fd_dir = f"/proc/{pid_dir}/fd"
                try:
                    for fd in os.listdir(fd_dir):
                        try:
                            link = os.readlink(f"{fd_dir}/{fd}")
                            if link == target:
                                return int(pid_dir)
                        except (OSError, ValueError):
                            continue
                except PermissionError:
                    continue
        except OSError:
            pass
        return None

    def _process_name(pid: int) -> str | None:
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            return None

    # --- macOS / fallback: lsof ---
    def _parse_lsof():
        import subprocess as _sp

        try:
            result = _sp.run(
                ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-F", "pcn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, _sp.TimeoutExpired):
            return

        current_pid = None
        current_name = None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                current_pid = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("c"):
                current_name = line[1:]
            elif line.startswith("n"):
                # e.g. "n*:8080" or "n127.0.0.1:3000" or "n[::1]:3000"
                addr = line[1:]
                colon_idx = addr.rfind(":")
                if colon_idx >= 0:
                    port_str = addr[colon_idx + 1:]
                    if port_str.isdigit():
                        port = int(port_str)
                        if port not in ports:
                            ports[port] = {
                                "port": port,
                                "pid": current_pid,
                                "process": current_name,
                            }

    # --- Windows: netstat ---
    def _parse_netstat():
        import subprocess as _sp

        try:
            result = _sp.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, _sp.TimeoutExpired):
            return

        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                local_addr = parts[1]
                colon_idx = local_addr.rfind(":")
                if colon_idx >= 0:
                    port_str = local_addr[colon_idx + 1:]
                    if port_str.isdigit():
                        port = int(port_str)
                        pid = int(parts[4]) if parts[4].isdigit() else None
                        if port not in ports:
                            ports[port] = {
                                "port": port,
                                "pid": pid,
                                "process": None,
                            }

    # Choose strategy
    if os.path.exists("/proc/net/tcp"):
        _parse_proc_net_tcp()
    elif platform.system() == "Windows":
        _parse_netstat()
    else:
        _parse_lsof()

    return sorted(ports.values(), key=lambda p: p["port"])


def get_descendant_pids(root_pid: int) -> set[int]:
    """Return all PIDs that are descendants of *root_pid* (exclusive).

    Strategy:
      - Linux: parse ``/proc/*/stat`` for parent PID
      - macOS / fallback: run ``ps -eo pid,ppid``
    """
    children: dict[int, list[int]] = {}

    if os.path.exists("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    stat = f.read().split()
                    ppid = int(stat[3])
                    children.setdefault(ppid, []).append(int(entry))
            except (FileNotFoundError, PermissionError, IndexError, ValueError):
                continue
    else:
        import subprocess as _sp

        try:
            result = _sp.run(
                ["ps", "-eo", "pid,ppid"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid, ppid = int(parts[0]), int(parts[1])
                        children.setdefault(ppid, []).append(pid)
                    except ValueError:
                        continue
        except (FileNotFoundError, _sp.TimeoutExpired):
            return set()

    descendants: set[int] = set()
    queue = list(children.get(root_pid, []))
    while queue:
        pid = queue.pop()
        if pid not in descendants:
            descendants.add(pid)
            queue.extend(children.get(pid, []))
    return descendants

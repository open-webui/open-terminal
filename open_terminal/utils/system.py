"""Read-only machine snapshots shared across viewers, scoped before returning processes."""

import os
import platform
import socket
import threading
import time
from pathlib import Path

import psutil

_started = time.monotonic()
_lock = threading.Lock()
_snapshot = None
_previous = None
_sampled_at = 0.0


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def _cgroup():
    """Read unified cgroup limits, including stricter visible ancestors."""
    root = Path('/sys/fs/cgroup')
    relative = next((line[3:] for line in _read('/proc/self/cgroup').splitlines() if line.startswith('0::')), '')
    directory = root / relative.lstrip('/')
    if not directory.is_relative_to(root) or '..' in directory.parts:
        return {}
    if not (directory / 'cpu.stat').exists():
        directory = root
    if not (directory / 'cpu.stat').exists():
        return {}
    result = {'path': str(directory)}
    for parent in [directory, *directory.parents]:
        if not parent.is_relative_to(root):
            break
        quota = _read(parent / 'cpu.max').split()
        if len(quota) == 2 and quota[0].isdigit() and quota[1].isdigit() and int(quota[1]) > 0:
            result['cores'] = min(result.get('cores', float('inf')), int(quota[0]) / int(quota[1]))
        limit = _read(parent / 'memory.max')
        current = _read(parent / 'memory.current')
        if (
            limit.isdigit()
            and int(limit) > 0
            and current.isdigit()
            and int(limit) < result.get('memory_total', float('inf'))
        ):
            result.update(memory_total=int(limit), memory_used=int(current))
    for line in _read(directory / 'cpu.stat').splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == 'usage_usec' and fields[1].isdigit():
            result['cpu_seconds'] = int(fields[1]) / 1_000_000
    return result


def _percent(used, total):
    return round(max(0, min(100, used / total * 100)), 1) if total > 0 else None


def _sample():
    global _previous
    now = time.monotonic()
    container = Path('/.dockerenv').exists() or Path('/run/.containerenv').exists() or bool(os.environ.get('container'))
    data = {
        'sampled_at': time.time(),
        'hostname': socket.gethostname(),
        'os': f'{platform.system()} {platform.release()}',
        'arch': platform.machine(),
        'environment': 'container' if container else 'host',
        'service_uptime_seconds': int(now - _started),
        'uptime_seconds': None,
        'cpu': {'percent': None, 'cores': psutil.cpu_count(), 'scope': 'host'},
        'memory': None,
        'network': None,
        'processes': None,
    }
    counters = {'time': now, 'processes': {}}
    try:
        data['uptime_seconds'] = max(0, int(time.time() - psutil.boot_time()))
    except (psutil.Error, OSError):
        pass
    try:
        cpu = psutil.cpu_times()._asdict()
        # guest time is already included in user/nice on Linux.
        counters['cpu_total'] = sum(v for k, v in cpu.items() if k not in ('guest', 'guest_nice'))
        counters['cpu_idle'] = cpu.get('idle', 0) + cpu.get('iowait', 0)
        if _previous and 'cpu_total' in _previous:
            total = counters['cpu_total'] - _previous['cpu_total']
            idle = counters['cpu_idle'] - _previous['cpu_idle']
            data['cpu']['percent'] = _percent(total - idle, total)
    except (psutil.Error, OSError):
        pass
    try:
        memory = psutil.virtual_memory()
        data['memory'] = {'total': memory.total, 'used': memory.total - memory.available, 'scope': 'host'}
    except (psutil.Error, OSError):
        pass
    group = _cgroup() if platform.system() == 'Linux' else {}
    if 'memory_total' in group:
        data['memory'] = {'total': group['memory_total'], 'used': group['memory_used'], 'scope': 'cgroup'}
    if 'cpu_seconds' in group and (container or 'cores' in group):
        cores = data['cpu']['cores'] or 1
        try:
            cores = len(psutil.Process().cpu_affinity()) or cores
        except (psutil.Error, OSError, AttributeError):
            pass
        cores = min(cores, group.get('cores', cores))
        counters['cgroup'] = group
        data['cpu'] = {'percent': None, 'cores': cores, 'scope': 'cgroup'}
        previous_group = (_previous or {}).get('cgroup', {})
        if previous_group.get('path') == group['path']:
            elapsed = now - _previous['time']
            data['cpu']['percent'] = _percent(group['cpu_seconds'] - previous_group['cpu_seconds'], elapsed * cores)
    try:
        interfaces = psutil.net_if_addrs()
        states = psutil.net_if_stats()
        data['network'] = [
            {
                'name': name,
                'up': states[name].isup if name in states else None,
                'addresses': [a.address for a in addresses if a.family in (socket.AF_INET, socket.AF_INET6)],
            }
            for name, addresses in interfaces.items()
        ]
    except (psutil.Error, OSError):
        pass
    try:
        processes = []
        for process in psutil.process_iter():
            try:
                with process.oneshot():
                    # Ownership and creation time are read afresh to handle exited/reused PIDs.
                    owner = process.username()
                    created = process.create_time()
                    times = process.cpu_times()
                    seconds = times.user + times.system
                    rss = process.memory_info().rss
                    name = process.name()
                key = (process.pid, created)
                counters['processes'][key] = seconds
                before = (_previous or {}).get('processes', {}).get(key)
                cpu = None if before is None else round(max(0, seconds - before) / (now - _previous['time']) * 100, 1)
                processes.append(
                    {'pid': process.pid, 'name': name, 'cpu_percent': cpu, 'memory_bytes': rss, 'owner': owner}
                )
            except (psutil.Error, OSError):
                continue
        data['processes'] = processes
    except (psutil.Error, OSError):
        pass
    _previous = counters
    return data


def collect_system_info(directory, username=None):
    global _snapshot, _sampled_at
    with _lock:
        if _snapshot is None or time.monotonic() - _sampled_at >= 2:
            _snapshot = _sample()
            _sampled_at = time.monotonic()
        result = dict(_snapshot)
    processes = result['processes']
    if processes is not None:
        # Filter before ranking so another user's process names and PIDs never escape.
        visible = [p for p in processes if username is None or p['owner'] == username]
        top = {p['pid']: p for p in sorted(visible, key=lambda p: p['cpu_percent'] or 0, reverse=True)[:10]}
        top.update({p['pid']: p for p in sorted(visible, key=lambda p: p['memory_bytes'], reverse=True)[:10]})
        result['processes'] = [{k: v for k, v in p.items() if k != 'owner'} for p in top.values()]
    result['process_scope'] = 'user' if username else 'environment'
    try:
        disk = psutil.disk_usage(directory)
        result['disk'] = {'path': directory, 'total': disk.total, 'used': disk.used, 'free': disk.free}
    except (psutil.Error, OSError):
        result['disk'] = None
    return result

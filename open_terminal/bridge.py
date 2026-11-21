"""Relay stdin/stdout to a local --ipc server.

A client that reaches this machine over SSH normally forwards straight to the
--ipc socket. When sshd refuses to forward Unix sockets
(AllowStreamLocalForwarding no), the client runs `open-terminal bridge <socket>`
over an exec channel instead and speaks HTTP over its stdin/stdout.

Standard library only, so it starts quickly: one bridge runs per connection.
"""

import os
import socket
import sys
import threading

CHUNK = 65536


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def relay(path: str) -> int:
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(path)
    except OSError as exc:
        print(f"open-terminal bridge: cannot connect to {path}: {exc.strerror or exc}", file=sys.stderr)
        return 1

    done = threading.Event()

    def downstream():
        # Server -> stdout. The connection is over when the server closes its side.
        try:
            while data := sock.recv(CHUNK):
                _write_all(1, data)
        except OSError:
            pass  # stdout closed: the client went away
        finally:
            done.set()

    def upstream():
        # stdin -> server. On EOF, half-close so the server still sends its reply.
        try:
            while data := os.read(0, CHUNK):
                sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # the server closed first; downstream ends the relay

    threading.Thread(target=downstream, daemon=True).start()
    threading.Thread(target=upstream, daemon=True).start()
    done.wait()
    sock.close()
    return 0

"""Daemon IPC: AF_UNIX socket, newline-delimited JSON. macOS only.

The socket is created 0700 (umask), so cross-USER access is blocked. Note the
boundary is per-user, not per-process: any process running as the same user can
connect and drive the daemon (which holds Accessibility + Screen Recording
grants). That is by design for a single-user machine — see the README security
model. A token would not help, since a same-user attacker could read it too.

The runtime dir (socket/pid) lives under /tmp to stay within the 104-byte
sun_path limit on macOS; /tmp itself is left untouched. The data dir
(~/.computer-harness) holds screen captures and is locked to 0700.
"""
import asyncio
import json
import os
import re
import socket
import tempfile
from pathlib import Path

NAME = os.environ.get("CH_NAME", "default")
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

_RUNTIME = Path(os.environ.get("CH_RUNTIME_DIR") or "/tmp")
_TMP = Path(os.environ.get("CH_TMP_DIR") or (Path.home() / ".computer-harness"))
_RUNTIME.mkdir(parents=True, exist_ok=True)  # /tmp: do NOT chmod (shared)


def _private_dir(path):
    """mkdir + force 0700 (mkdir's mode is masked by umask, so chmod after)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


_private_dir(_TMP)  # screen captures live here — owner-only


def _check(name):
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid CH_NAME {name!r}: must match [A-Za-z0-9_-]{{1,64}}")
    return name


def _stem(name):
    _check(name)
    return f"ch-{name}"


def sock_path(name=NAME):
    return _RUNTIME / f"{_stem(name)}.sock"


def pid_path(name=NAME):
    return _RUNTIME / f"{_stem(name)}.pid"


def log_path(name=NAME):
    return _TMP / f"{_stem(name)}.log"


def screenshot_dir():
    return _private_dir(_TMP / "screenshots")


def recording_dir():
    return _private_dir(_TMP / "recordings")


def connect(name=NAME, timeout=2.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path(name)))
    return s


def request(sock, req, timeout=120.0):
    sock.settimeout(timeout)
    sock.sendall((json.dumps(req) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        data += chunk
    return json.loads(data or b"{}")


def call(req, name=NAME, timeout=120.0):
    """Open, send one request, read reply, close."""
    s = connect(name)
    try:
        return request(s, req, timeout=timeout)
    finally:
        try:
            s.close()
        except OSError:
            pass


def ping(name=NAME, timeout=1.5):
    try:
        s = connect(name, timeout=timeout)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    try:
        resp = request(s, {"meta": "ping"}, timeout=timeout)
        return isinstance(resp, dict) and resp.get("pong") is True
    except (OSError, ValueError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


async def serve(name, handler):
    path = str(sock_path(name))
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(handler, path=path)
    finally:
        os.umask(old)
    async with server:
        await asyncio.Event().wait()


def cleanup(name=NAME):
    for p in (sock_path(name), pid_path(name)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

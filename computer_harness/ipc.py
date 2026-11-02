"""Daemon IPC: AF_UNIX socket (macOS) or loopback TCP + token (Windows),
newline-delimited JSON.

The socket is created 0700 (umask), so cross-USER access is blocked. Note the
boundary is per-user, not per-process: any process running as the same user can
connect and drive the daemon (which holds Accessibility + Screen Recording
grants). That is by design for a single-user machine — see the README security
model. A token would not help, since a same-user attacker could read it too.

The runtime dir (socket/pid) lives under /tmp to stay within the 104-byte
sun_path limit on macOS; /tmp itself is left untouched. The data dir
(~/.computer-harness) holds screen captures and is locked to 0700.

Windows: asyncio has no AF_UNIX server there, so the daemon listens on
127.0.0.1 at a random port. Any local user can reach a loopback port, so the
client must send a random token first; port and token live in
~/.computer-harness/ch-<name>.port, which the profile ACL keeps owner-only.
Same boundary as macOS: same-user processes can read the token.
"""
import asyncio
import hmac
import json
import os
import re
import secrets
import socket
import sys
import tempfile
from pathlib import Path

WINDOWS = sys.platform == "win32"

NAME = os.environ.get("CH_NAME", "default")
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

_TMP = Path(os.environ.get("CH_TMP_DIR") or (Path.home() / ".computer-harness"))
_RUNTIME = Path(os.environ.get("CH_RUNTIME_DIR") or (_TMP if WINDOWS else "/tmp"))
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


def port_path(name=NAME):
    """Windows only: "<port> <token>" of the daemon's loopback listener."""
    return _RUNTIME / f"{_stem(name)}.port"


def log_path(name=NAME):
    return _TMP / f"{_stem(name)}.log"


def screenshot_dir():
    return _private_dir(_TMP / "screenshots")


def recording_dir():
    return _private_dir(_TMP / "recordings")


def connect(name=NAME, timeout=2.0):
    if WINDOWS:
        port, token = port_path(name).read_text().split()
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        s.sendall((token + "\n").encode())
        return s
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
    except (FileNotFoundError, ConnectionRefusedError, OSError, ValueError):
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
    if WINDOWS:
        await _serve_tcp(name, handler)
        return
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


async def _serve_tcp(name, handler):
    token = secrets.token_hex(32)

    async def guarded(reader, writer):
        line = await reader.readline()
        if not hmac.compare_digest(line.strip(), token.encode()):
            writer.close()
            return
        await handler(reader, writer)

    server = await asyncio.start_server(guarded, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    port_path(name).write_text(f"{port} {token}")
    async with server:
        await asyncio.Event().wait()


def cleanup(name=NAME):
    for p in (sock_path(name), pid_path(name), port_path(name)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

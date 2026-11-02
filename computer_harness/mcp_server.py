"""MCP server for computer-harness: the helper verbs as MCP tools.

    computer-harness mcp                         stdio, for a client on this machine
    computer-harness mcp --http --host <lan-ip> --self-signed
                                                 HTTPS on the LAN, bearer token required

Unlike the heredoc CLI, clients cannot run arbitrary Python: they get the
listed tools only. Foreground tools (real cursor and keyboard) are left out
unless --allow-foreground is given.

Over HTTP every request needs `Authorization: Bearer <token>`. The token comes
from CH_MCP_TOKEN or ~/.computer-harness/mcp-token (created on first run,
owner-only). A non-loopback --host requires TLS: --cert/--key, or
--self-signed to generate a certificate for this host. The Host header must
be the bound address, this machine's hostname, or an --allowed-host.
"""
import argparse
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys

from . import helpers, ipc
from .admin import ensure_daemon

INSTRUCTIONS = """Drive native macOS / Windows apps on this machine without disturbing its user.
Start with get_app_state(app): it returns the numbered element tree and an annotated
window screenshot. Then click / set_value / perform_action by element_index or label.
Use wait_for instead of sleeping. Background tools never move the cursor or steal focus."""


def _ok(fn):
    """Run a helper with a live daemon (it restarts if it died)."""
    ensure_daemon()
    return fn()


def _image(path):
    from mcp.server.mcpserver import Image
    return Image(path=path)


def build_server(allow_foreground=False, **server_kwargs):
    from mcp.server import MCPServer

    mcp = MCPServer("computer-harness", instructions=INSTRUCTIONS, **server_kwargs)

    # --- inspection --------------------------------------------------------
    @mcp.tool()
    def screen_info() -> dict:
        """Main display size in click coordinates and its scale factor."""
        return _ok(helpers.screen_info)

    @mcp.tool()
    def list_apps() -> list:
        """Running apps with a window: name, bundle_id (exe path on Windows), pid, active."""
        return _ok(helpers.list_apps)

    @mcp.tool()
    def list_windows(app: str | None = None) -> list:
        """On-screen windows: window_id, owner, pid, title, x/y/w/h."""
        return _ok(lambda: helpers.list_windows(app))

    @mcp.tool()
    def frontmost_app() -> dict | None:
        """The app the user is working in."""
        return _ok(helpers.frontmost_app)

    # --- background (non-disturbing) ----------------------------------------
    @mcp.tool()
    def get_app_state(app: str, screenshot: bool = True, interactive_only: bool = False,
                      diff: bool = False, max_elements: int = 400) -> list:
        """Snapshot an app's main window in the background: the numbered element tree
        (`text`) and, with screenshot=True, the window image with each element's index
        drawn on it. Call this before click/set_value/perform_action/find.
        interactive_only drops elements you cannot act on; diff lists what changed
        since the previous snapshot of this app."""
        st = _ok(lambda: helpers.get_app_state(app, screenshot=screenshot, max_elements=max_elements,
                                               interactive_only=interactive_only, diff=diff))
        summary = {k: st[k] for k in ("app", "pid", "window", "window_bounds", "text") if k in st}
        if "diff" in st:
            summary["diff"] = st["diff"]
        out = [json.dumps(summary, ensure_ascii=False, indent=1)]
        shot = st.get("screenshot") or {}
        path = shot.get("annotated_path") or shot.get("path")
        if path:
            out.append(_image(path))
        return out

    @mcp.tool()
    def find(app: str, label: str | None = None, role: str | None = None) -> list:
        """Elements of the last snapshot matching label (exact, then substring) and role
        (a kind such as "button", "text", "checkbox", or a native role)."""
        return _ok(lambda: helpers.find(app, label=label, role=role))

    @mcp.tool()
    def click(app: str, element_index: int | None = None, label: str | None = None,
              role: str | None = None, method: str = "ax") -> dict:
        """Press an element from the last snapshot, by element_index or label (+ role).
        method="ax" acts in the background. method="foreground" moves the real cursor
        and needs the server started with --allow-foreground."""
        if method != "ax" and not allow_foreground:
            raise ValueError("method='foreground' is disabled; start the server with --allow-foreground")
        return _ok(lambda: helpers.click(app, element_index, method=method, label=label, role=role))

    @mcp.tool()
    def set_value(app: str, value: str, element_index: int | None = None,
                  label: str | None = None, role: str | None = None) -> dict:
        """Set a text field's value directly, without keystrokes, in the background."""
        return _ok(lambda: helpers.set_value(app, element_index, value=value, label=label, role=role))

    @mcp.tool()
    def perform_action(app: str, action: str, element_index: int | None = None,
                       label: str | None = None, role: str | None = None) -> dict:
        """Run an accessibility action on an element: AXPress, AXShowMenu, AXIncrement, ...
        (Windows also: Invoke, Toggle, Select, Expand, Collapse)."""
        return _ok(lambda: helpers.perform_action(app, element_index, action=action, label=label, role=role))

    @mcp.tool()
    def wait_for(app: str, label: str | None = None, role: str | None = None,
                 timeout: float = 10.0) -> dict:
        """Re-snapshot the app until an element matches; returns it. Use instead of sleeping."""
        return _ok(lambda: helpers.wait_for(app, label=label, role=role, timeout=timeout))

    @mcp.tool()
    def wait_window(title: str, timeout: float = 10.0) -> dict:
        """Wait until a window whose title contains `title` is on screen."""
        return _ok(lambda: helpers.wait_window(title, timeout=timeout))

    @mcp.tool()
    def capture_window(app: str | None = None, window_id: int | None = None) -> list:
        """Image of one window, captured in the background (covered is fine, minimized is not)."""
        r = _ok(lambda: helpers.capture_window(app=app, window_id=window_id))
        return [json.dumps({"width": r["width"], "height": r["height"]}), _image(r["path"])]

    @mcp.tool()
    def capture_screenshot(x: int | None = None, y: int | None = None,
                           width: int | None = None, height: int | None = None) -> list:
        """Image of the main screen, or of the region x/y/width/height. 1 pixel = 1 click point."""
        region = (x, y, width, height) if None not in (x, y, width, height) else None
        r = _ok(lambda: helpers.capture_screenshot(region=region))
        return [json.dumps({"width": r["width"], "height": r["height"]}), _image(r["path"])]

    if not allow_foreground:
        return mcp

    # --- foreground (real cursor and keyboard; interrupts the user) -----------
    @mcp.tool()
    def activate_app(app: str) -> dict:
        """FOREGROUND: bring an app to the front (takes focus from the user)."""
        return _ok(lambda: helpers.activate_app(app))

    @mcp.tool()
    def click_at_xy(x: float, y: float, button: str = "left", count: int = 1) -> dict:
        """FOREGROUND: real mouse click at screen coordinates (moves the cursor)."""
        return _ok(lambda: helpers.click_at_xy(x, y, button=button, count=count))

    @mcp.tool()
    def type_text(text: str) -> dict:
        """FOREGROUND: type text into whatever has keyboard focus. Prefer set_value."""
        return _ok(lambda: helpers.type_text(text))

    @mcp.tool()
    def press_key(key: str) -> dict:
        """FOREGROUND: key chord such as "cmd+c", "ctrl+shift+t", "Return", "Escape"."""
        return _ok(lambda: helpers.press_key(key))

    @mcp.tool()
    def drag(from_x: float, from_y: float, to_x: float, to_y: float) -> dict:
        """FOREGROUND: drag with the left button between screen coordinates."""
        return _ok(lambda: helpers.drag(from_x, from_y, to_x, to_y))

    @mcp.tool()
    def scroll(dx: int = 0, dy: int = 0) -> dict:
        """FOREGROUND: scroll at the cursor, in lines. Positive dy scrolls up."""
        return _ok(lambda: helpers.scroll(dx=dx, dy=dy))

    return mcp


# ---------------------------------------------------------------------------
# HTTP: token, host allowlist, TLS
# ---------------------------------------------------------------------------

def _private_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def load_token():
    token = os.environ.get("CH_MCP_TOKEN", "").strip()
    if token:
        return token
    path = ipc._TMP / "mcp-token"
    if not path.exists():
        _private_file(path, secrets.token_urlsafe(32))
    return path.read_text().strip()


class BearerAuth:
    """ASGI middleware: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def local_names(host, extra=()):
    """Names and IPs clients may use for this server: the bound address (every
    local IP for 0.0.0.0), the hostname, <hostname>.local, and `extra`."""
    full = socket.gethostname()
    short = full.split(".")[0]
    names = {"127.0.0.1", "localhost", full, short, f"{short}.local", *extra}
    if host in ("0.0.0.0", "::"):
        try:
            names |= set(socket.gethostbyname_ex(full)[2])
        except OSError:
            pass
    else:
        names.add(host)
    return sorted(n for n in names if n)


def allowed_hosts(host, extra=()):
    return [h for n in local_names(host, extra) for h in (n, f"{n}:*")]


def self_signed(host, extra=()):
    """Create (or reuse) a certificate for this host. Returns (cert, key, sha256)."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = ipc._TMP / "mcp-tls"
    cert_p, key_p = d / "cert.pem", d / "key.pem"
    every = local_names(host, extra)
    names = [n for n in every if not _ip(n)]
    ips = [n for n in every if _ip(n)]

    if cert_p.exists() and key_p.exists():
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = set(san.get_values_for_type(x509.DNSName)) | {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        fresh = cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        if set(names) | set(ips) <= have and fresh:
            return cert_p, key_p, cert.fingerprint(hashes.SHA256()).hex(":")

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "computer-harness")])  # names live in the SAN
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]),
            critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    _private_file(key_p, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()).decode())
    _private_file(cert_p, cert.public_bytes(serialization.Encoding.PEM).decode())
    return cert_p, key_p, cert.fingerprint(hashes.SHA256()).hex(":")


def _ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def build_http_app(mcp, token, host, extra_hosts=()):
    from mcp.server.transport_security import TransportSecuritySettings
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts(host, extra_hosts),
                                                     allowed_origins=[]),
        host=host,
    )
    return BearerAuth(app, token)


def main(argv=None):
    p = argparse.ArgumentParser(prog="computer-harness mcp", description=__doc__.split("\n\n")[0])
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (a LAN IP, or 0.0.0.0)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cert", help="TLS certificate (PEM)")
    p.add_argument("--key", help="TLS private key (PEM)")
    p.add_argument("--self-signed", action="store_true", help="generate a certificate for this host")
    p.add_argument("--allowed-host", action="append", default=[],
                   help="extra Host header value clients use (DNS name, IP); repeatable")
    p.add_argument("--allow-foreground", action="store_true",
                   help="also expose tools that move the real cursor and type")
    a = p.parse_args(argv)

    mcp = build_server(allow_foreground=a.allow_foreground)
    if not a.http:
        ensure_daemon()
        mcp.run()  # stdio
        return

    if a.self_signed and (a.cert or a.key):
        p.error("use --self-signed or --cert/--key, not both")
    if not _is_loopback(a.host) and not (a.self_signed or (a.cert and a.key)):
        p.error("a non-loopback --host needs TLS: --self-signed, or --cert and --key")
    fingerprint = None
    if a.self_signed:
        a.cert, a.key, fingerprint = self_signed(a.host, a.allowed_host)

    ensure_daemon()
    token = load_token()
    app = build_http_app(mcp, token, a.host, a.allowed_host)
    scheme = "https" if a.cert else "http"
    shown = socket.gethostname().split(".")[0] + ".local" if a.host == "0.0.0.0" else a.host
    url = f"{scheme}://{shown}:{a.port}/mcp"
    print(f"computer-harness MCP on {url}"
          f"{' with foreground tools' if a.allow_foreground else ''}", file=sys.stderr)
    print(f"  token: {'CH_MCP_TOKEN' if os.environ.get('CH_MCP_TOKEN') else ipc._TMP / 'mcp-token'}",
          file=sys.stderr)
    if fingerprint:
        print(f"  certificate: {a.cert}\n  sha256: {fingerprint}", file=sys.stderr)
        print("  clients must trust that certificate (Node: NODE_EXTRA_CA_CERTS=<cert.pem>)", file=sys.stderr)
    print(f'  claude mcp add --transport http computer-harness {url} '
          f'--header "Authorization: Bearer $(cat <token file>)"', file=sys.stderr)

    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, ssl_certfile=a.cert, ssl_keyfile=a.key,
                log_level="warning")


if __name__ == "__main__":
    main()

"""End-to-end: serve MCP over HTTPS on this machine's LAN IP and drive a real
app through it (Windows: a WinForms window, macOS: TextEdit).

    python tests/smoke_mcp.py
"""
import base64
import io
import os
import socket
import ssl
import subprocess
import sys
import time

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from computer_harness import ipc

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(detail)[:200]}")
    if not ok:
        failures.append(name)


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
        return s.getsockname()[0]


def open_app():
    """Start the test app; return (app name for tools, cleanup)."""
    if sys.platform == "win32":
        sys.path.insert(0, os.path.dirname(__file__))
        from smoke_windows import FORM, TITLE
        p = subprocess.Popen(["powershell", "-NoProfile", "-Command", FORM])
        return TITLE, p.kill
    doc = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ch-mcp.txt")
    with open(doc, "w") as f:
        f.write("start")
    subprocess.run(["open", "-a", "TextEdit", doc], check=True, timeout=30)
    return "TextEdit", lambda: subprocess.run(["pkill", "-x", "TextEdit"])


def text_of(result):
    return "".join(b.text for b in result.content if b.type == "text")


async def drive(url, cert, token, app):
    ctx = ssl.create_default_context(cafile=cert)
    async with httpx2.AsyncClient(verify=ctx) as anon:
        r = await anon.post(url, json={})
        check("no token -> 401", r.status_code == 401, r.status_code)

    http = httpx2.AsyncClient(verify=ctx, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    async with http, Client(streamable_http_client(url, http_client=http)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        check("foreground tools listed", {"click_at_xy", "type_text"} <= names, sorted(names))

        win = await c.call_tool("wait_window", {"title": "CH Smoke" if app == "CH Smoke" else "ch-mcp",
                                                "timeout": 20})
        check("wait_window", not win.is_error, text_of(win))
        st = await c.call_tool("get_app_state", {"app": app})
        img = [b for b in st.content if b.type == "image"]
        check("get_app_state text", not st.is_error and '"text"' in text_of(st), text_of(st)[:120])
        size = Image.open(io.BytesIO(base64.b64decode(img[0].data))).size if img else None
        check("annotated image returned", bool(img) and size[0] > 50, size)

        if app == "CH Smoke":
            r = await c.call_tool("click", {"app": app, "label": "Press me"})
            check("click by label", not r.is_error and "Invoke" in text_of(r), text_of(r))
            r = await c.call_tool("wait_for", {"app": app, "label": "Pressed", "timeout": 5})
            check("wait_for", not r.is_error, text_of(r))
        else:
            r = await c.call_tool("set_value", {"app": app, "role": "text", "value": "via mcp"})
            check("set_value by role", not r.is_error, text_of(r))
            await c.call_tool("get_app_state", {"app": app, "screenshot": False})
            r = await c.call_tool("find", {"app": app, "label": "via mcp"})
            check("value read back", "via mcp" in text_of(r), text_of(r))


def main():
    host, port = lan_ip(), 8765
    check("LAN address", not host.startswith("127."), host)
    os.environ["CH_MCP_TOKEN"] = "smoke-" + os.urandom(8).hex()
    server = subprocess.Popen([sys.executable, "-m", "computer_harness.run", "mcp", "--http",
                               "--host", host, "--port", str(port), "--self-signed",
                               "--allow-foreground"], stderr=subprocess.PIPE, text=True)
    app, close_app = open_app()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection((host, port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        cert = str(ipc._TMP / "mcp-tls" / "cert.pem")
        anyio.run(drive, f"https://{host}:{port}/mcp", cert, os.environ["CH_MCP_TOKEN"], app)
    finally:
        close_app()
        server.terminate()
        try:
            print(server.communicate(timeout=10)[1][-1500:])
        except subprocess.TimeoutExpired:
            server.kill()
    print(f"\n{len(failures)} failed: {failures}" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

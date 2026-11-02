"""MCP server tests with a fake daemon: tools, images, foreground gating, and
the HTTPS + bearer token + Host checks on a real uvicorn server.
Needs the [mcp] extra: pip install -e ".[mcp]" pytest"""
import socket
import ssl
import threading
import time

import pytest
from PIL import Image as PILImage

mcp_sdk = pytest.importorskip("mcp")
import anyio  # noqa: E402
import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from computer_harness import helpers, mcp_server  # noqa: E402

FOREGROUND = {"activate_app", "click_at_xy", "type_text", "press_key", "drag", "scroll"}


@pytest.fixture
def fake_daemon(monkeypatch, tmp_path):
    shot = tmp_path / "w.png"
    PILImage.new("RGB", (40, 20), "white").save(shot)
    calls = []

    def call(op, timeout=120.0, **args):
        calls.append((op, args))
        if op == "get_app_state":
            return {"app": "Notes", "pid": 7, "window": "Doc", "window_bounds": {}, "elements": [],
                    "text": "[0] button “Save”", "screenshot": {"path": str(shot), "annotated_path": str(shot)}}
        if op == "click":
            return {"clicked": 0, "via": "AXPress", "focus_restored": False}
        return {}

    monkeypatch.setattr(helpers, "_call", call)
    monkeypatch.setattr(mcp_server, "ensure_daemon", lambda: None)
    return calls


async def _tools(server):
    async with Client(server) as c:
        return {t.name for t in (await c.list_tools()).tools}


def test_foreground_tools_are_opt_in(fake_daemon):
    plain = anyio.run(_tools, mcp_server.build_server())
    full = anyio.run(_tools, mcp_server.build_server(allow_foreground=True))
    assert {"get_app_state", "click", "set_value", "find", "wait_for", "capture_screenshot"} <= plain
    assert not plain & FOREGROUND
    assert FOREGROUND <= full


def test_get_app_state_returns_tree_and_image(fake_daemon):
    async def go():
        async with Client(mcp_server.build_server()) as c:
            return await c.call_tool("get_app_state", {"app": "Notes"})
    r = anyio.run(go)
    kinds = [b.type for b in r.content]
    assert kinds == ["text", "image"], kinds
    assert "“Save”" in r.content[0].text and r.content[1].mime_type == "image/png"


def test_foreground_click_needs_the_flag(fake_daemon):
    async def go(server):
        async with Client(server) as c:
            return await c.call_tool("click", {"app": "Notes", "label": "Save", "method": "foreground"})
    assert anyio.run(go, mcp_server.build_server()).is_error
    assert not anyio.run(go, mcp_server.build_server(allow_foreground=True)).is_error
    assert fake_daemon[-1] == ("click", {"app": "Notes", "element_index": None, "method": "foreground",
                                          "button": "left", "count": 1, "label": "Save", "role": None})


def test_lan_bind_requires_tls():
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--host", "192.168.1.50"])


# --- HTTPS server ------------------------------------------------------------

@pytest.fixture
def https_server(fake_daemon, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server.ipc, "_TMP", tmp_path)
    monkeypatch.setenv("CH_MCP_TOKEN", "s3cret-token")
    cert, key, fp = mcp_server.self_signed("127.0.0.1")
    app = mcp_server.build_http_app(mcp_server.build_server(), mcp_server.load_token(), "127.0.0.1")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, ssl_certfile=str(cert),
                                           ssl_keyfile=str(key), log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}/mcp", str(cert), fp
    server.should_exit = True
    t.join(5)


def test_https_token_and_host_checks(https_server):
    url, cert, fp = https_server
    assert len(fp.split(":")) == 32
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}
    hdrs = {"accept": "application/json, text/event-stream"}
    with httpx2.Client(verify=ssl.create_default_context(cafile=cert)) as h:
        assert h.post(url, json=init, headers=hdrs).status_code == 401
        bad = {**hdrs, "authorization": "Bearer nope"}
        assert h.post(url, json=init, headers=bad).status_code == 401
        good = {**hdrs, "authorization": "Bearer s3cret-token"}
        assert h.post(url, json=init, headers={**good, "host": "evil.example"}).status_code == 421
    with pytest.raises(Exception):  # the self-signed cert is not trusted by default
        httpx2.get(url)


def test_https_client_calls_tools(https_server):
    url, cert, _ = https_server

    async def go():
        http = httpx2.AsyncClient(verify=ssl.create_default_context(cafile=cert), headers={"Authorization": "Bearer s3cret-token"})
        async with http, Client(streamable_http_client(url, http_client=http)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("click", {"app": "Notes", "label": "Save"})
            return names, r
    names, r = anyio.run(go)
    assert "click" in names and not names & FOREGROUND
    assert not r.is_error and '"AXPress"' in r.content[0].text

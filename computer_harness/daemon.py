"""Computer-harness daemon.

Holds the Accessibility + Screen Recording TCC grants (stable process identity),
warms pyobjc once, and caches the last get_app_state() per app pid so
element_index / label clicks resolve against a real snapshot. One request per
connection, JSON in / JSON out.
"""
import asyncio
import json
import os
import sys
import time
import traceback

from . import admin, common, ipc

if sys.platform == "win32":
    from . import engine_win as engine
else:
    from . import engine

_START = time.time()
_LAST_STATE = {}  # app pid -> full get_app_state() result
_KEEP_FILES = int(os.environ.get("CH_KEEP_FILES", "500"))
_KEEP_DAYS = float(os.environ.get("CH_KEEP_DAYS", "7"))


def _prune():
    """Captures may hold secrets: cap how many and how old (0 = no limit)."""
    for d in (ipc.screenshot_dir(), ipc.recording_dir()):
        common.prune(d, _KEEP_FILES, _KEEP_DAYS)


def _cached(a):
    """(pid, snapshot, element index) for a request naming app + element."""
    pid = engine.app_pid(a.get("app", ""))
    st = _LAST_STATE.get(pid)
    if not st:
        raise ValueError("no cached app state; call get_app_state(app) first")
    idx = common.resolve_index(st["elements"], a.get("element_index"), a.get("label"), a.get("role"))
    return pid, st, idx


def _app_state(a):
    st = engine.get_app_state(
        a["app"], max_elements=a.get("max_elements", 400), capture=a.get("screenshot", True),
    )
    for e in st["elements"]:
        e["kind"] = common.kind_of(e["role"])
    st["text"] = common.render_text(st["elements"])
    shot = st.get("screenshot")
    if shot and a.get("annotate", True):
        marked = shot["path"][:-4] + "-marked.png"
        shot["annotated_path"] = common.annotate(shot["path"], st["elements"], marked)
    previous = _LAST_STATE.get(st["pid"])
    _LAST_STATE[st["pid"]] = st
    _prune()
    return common.shape_state(st, previous, a.get("interactive_only", False), a.get("diff", False))


def _handle(req):
    meta = req.get("meta")
    if meta == "ping":
        return {"pong": True, "pid": os.getpid(), "uptime": round(time.time() - _START, 1)}
    if meta == "shutdown":
        return {"ok": True, "shutdown": True}

    op = req.get("op")
    a = req.get("args", {}) or {}

    if op == "permissions":
        perms = {
            "accessibility": engine.has_accessibility(),
            "screen_recording": engine.has_screen_recording(),
        }
        if hasattr(engine, "is_elevated"):
            perms["elevated"] = engine.is_elevated()
        return perms
    if op == "screen_info":
        w, h = engine.logical_size()
        return {"width": w, "height": h, "scale": engine.backing_scale()}
    if op == "list_apps":
        return {"apps": engine.list_apps()}
    if op == "list_windows":
        pid = a.get("pid")
        if pid is None and a.get("app"):
            pid = engine.app_pid(a["app"])
        return {"windows": engine.list_windows(pid=pid)}
    if op == "activate_app":
        return engine.activate_app(a["app"])
    if op == "frontmost_app":
        return {"app": engine.frontmost_app()}

    if op == "capture_screenshot":
        ts = int(time.time() * 1000)
        path = a.get("path") or str(ipc.screenshot_dir() / f"shot-{ts}.png")
        res = engine.capture_screenshot(
            path, region=a.get("region"), resize_logical=a.get("resize_logical", True)
        )
        if a.get("data_url"):
            res["data_url"] = common.screenshot_data_url(path)
        _prune()
        return res

    if op == "record_start":
        ts = int(time.time() * 1000)
        ext = getattr(engine, "REC_EXT", ".mov")
        path = a.get("path") or str(ipc.recording_dir() / f"rec-{ts}{ext}")
        return engine.start_recording(
            a.get("name", "screen"), path, seconds=a.get("seconds"),
            region=a.get("region"), display=a.get("display", 1),
            show_clicks=a.get("show_clicks", False), audio=a.get("audio", False),
        )
    if op == "record_stop":
        return engine.stop_recording(a.get("name", "screen"))
    if op == "record_status":
        return {"recordings": engine.recording_status()}

    if op == "capture_window":
        ts = int(time.time() * 1000)
        path = a.get("path") or str(ipc.screenshot_dir() / f"window-{ts}.png")
        res = engine.capture_window(int(a["window_id"]), path)
        if res is None:
            raise RuntimeError("window capture returned no image (window_id gone? minimized?)")
        _prune()
        return res

    if op == "get_app_state":
        return _app_state(a)

    if op == "find":
        pid = engine.app_pid(a.get("app", ""))
        st = _LAST_STATE.get(pid)
        if not st:
            raise ValueError("no cached app state; call get_app_state(app) first")
        return {"elements": common.find_elements(st["elements"], a.get("label"), a.get("role"))}

    if op == "click":
        if a.get("element_index") is not None or a.get("label") is not None or a.get("role") is not None:
            pid, st, idx = _cached(a)
            res = engine.click_element(
                pid, st["elements"], idx,
                button=a.get("button", "left"), count=int(a.get("count", 1)),
                method=a.get("method", "ax"),
            )
            res.setdefault("focus_restored", False)
            return res
        engine.click(a["x"], a["y"], button=a.get("button", "left"), count=int(a.get("count", 1)))
        return {"clicked": [a["x"], a["y"]], "via": "foreground"}

    if op == "set_value":
        pid, st, idx = _cached(a)
        res = engine.set_element_value(pid, st["elements"], idx, a["value"])
        res.setdefault("focus_restored", False)
        return res

    if op == "move_mouse":
        engine.move_mouse(a["x"], a["y"])
        return {"at": [a["x"], a["y"]]}
    if op == "drag":
        engine.drag(a["from_x"], a["from_y"], a["to_x"], a["to_y"],
                    button=a.get("button", "left"), steps=int(a.get("steps", 25)))
        return {"from": [a["from_x"], a["from_y"]], "to": [a["to_x"], a["to_y"]]}
    if op == "scroll":
        engine.scroll(dx=int(a.get("dx", 0)), dy=int(a.get("dy", 0)))
        return {"dx": a.get("dx", 0), "dy": a.get("dy", 0)}
    if op == "type_text":
        engine.type_text(a["text"], per_char_delay=a.get("per_char_delay", 0.0))
        return {"typed": len(a["text"])}
    if op == "press_key":
        engine.press_key(a["key"])
        return {"pressed": a["key"]}
    if op == "perform_action":
        pid, st, idx = _cached(a)
        res = engine.perform_action(pid, st["elements"], idx, a["action"])
        res.setdefault("focus_restored", False)
        return res

    raise ValueError(f"unknown op {op!r}")


async def _client(reader, writer):
    try:
        line = await reader.readline()
        if not line:
            return
        req = json.loads(line)
        try:
            resp = _handle(req)
        except Exception as e:
            resp = {"error": str(e), "type": type(e).__name__, "trace": traceback.format_exc()}
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        if isinstance(resp, dict) and resp.get("shutdown"):
            def _bye():
                engine.stop_all_recordings()
                os._exit(0)
            asyncio.get_event_loop().call_later(0.1, _bye)
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _main(name):
    """Serve until shutdown. Returns False, touching nothing, when another live
    daemon already owns `name`."""
    other = admin.daemon_pid(name)
    if other and other != os.getpid():
        print(f"daemon {name!r} already running as pid {other}; exiting", file=sys.stderr)
        return False
    ipc.pid_path(name).write_text(str(os.getpid()))
    _prune()
    await ipc.serve(name, _client)


def run(name=None):
    name = name or ipc.NAME
    # Owner-only for everything this process (and its screencapture children)
    # creates: captures may contain on-screen secrets. Dirs are 0700 via ipc.
    os.umask(0o077)
    owner = True
    try:
        owner = asyncio.run(_main(name)) is not False
    finally:
        if owner:  # never remove the files of the daemon that refused us
            engine.stop_all_recordings()
            ipc.cleanup(name)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)

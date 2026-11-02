"""Computer-harness daemon.

Holds the Accessibility + Screen Recording TCC grants (stable process identity),
warms pyobjc once, and caches the last get_app_state() per app so element_index
clicks resolve against a real snapshot. One request per connection, JSON in /
JSON out.
"""
import asyncio
import json
import os
import sys
import time
import traceback

from . import ipc

if sys.platform == "win32":
    from . import engine_win as engine
else:
    from . import engine

_START = time.time()
_LAST_STATE = {}  # app identifier (lowercased) -> get_app_state() result


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
            res["data_url"] = engine.screenshot_data_url(path)
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
            raise RuntimeError("window capture returned no image (window_id gone?)")
        return res

    if op == "get_app_state":
        st = engine.get_app_state(
            a["app"], max_elements=a.get("max_elements", 400),
            capture=a.get("screenshot", True),
        )
        _LAST_STATE[str(a["app"]).lower()] = st
        return st

    if op == "click":
        if a.get("element_index") is not None:
            key = str(a.get("app", "")).lower()
            st = _LAST_STATE.get(key)
            if not st:
                raise ValueError("no cached app state; call get_app_state(app) first")
            return engine.click_element(
                st["pid"], st["elements"], int(a["element_index"]),
                button=a.get("button", "left"), count=int(a.get("count", 1)),
                method=a.get("method", "ax"),
            )
        engine.click(a["x"], a["y"], button=a.get("button", "left"), count=int(a.get("count", 1)))
        return {"clicked": [a["x"], a["y"]], "via": "foreground"}

    if op == "set_value":
        key = str(a.get("app", "")).lower()
        st = _LAST_STATE.get(key)
        if not st:
            raise ValueError("no cached app state; call get_app_state(app) first")
        return engine.set_element_value(st["pid"], st["elements"], int(a["element_index"]), a["value"])

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
        key = str(a.get("app", "")).lower()
        st = _LAST_STATE.get(key)
        if not st:
            raise ValueError("no cached app state; call get_app_state(app) first")
        return engine.perform_action(st["pid"], st["elements"], int(a["element_index"]), a["action"])

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
    ipc.pid_path(name).write_text(str(os.getpid()))
    await ipc.serve(name, _client)


def run(name=None):
    name = name or ipc.NAME
    # Owner-only for everything this process (and its screencapture children)
    # creates: captures may contain on-screen secrets. Dirs are 0700 via ipc.
    os.umask(0o077)
    try:
        asyncio.run(_main(name))
    finally:
        engine.stop_all_recordings()
        ipc.cleanup(name)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)

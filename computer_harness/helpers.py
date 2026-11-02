"""Model-facing helpers, pre-imported into every heredoc.

Two tiers of control:

  NON-DISTURBING (default) — operate on a target app's window in the background
  via the accessibility API + per-window capture. No cursor movement, no focus
  steal; the user keeps working in the foreground. Verbs: get_app_state, click
  (by element_index), set_value, perform_action, capture_window, list_windows.

  FOREGROUND (explicit, interrupts the user) — global CGEvent synthesis that
  drives whatever is frontmost and moves the real cursor. For canvas apps,
  games, or anything with no accessibility actions. Verbs: click_at_xy,
  type_text, press_key, drag, scroll, move_mouse, capture_screenshot.

Naming mirrors browser-harness so muscle memory carries over. Window-relative
element coords (`rel`) map 1:1 to get_app_state()'s window screenshot; global
`center` coords map 1:1 to a full-screen capture_screenshot().
"""
from . import ipc


def _call(op, timeout=120.0, **args):
    resp = ipc.call({"op": op, "args": args}, timeout=timeout)
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(f"{resp.get('type', 'Error')}: {resp['error']}")
    return resp


# --- inspection -----------------------------------------------------------

def screen_info():
    """Main display size in logical points + backing scale (the page_info analog)."""
    return _call("screen_info")


def list_apps():
    """Running apps with a UI (name, bundle_id, pid, active). Active app first."""
    return _call("list_apps")["apps"]


def list_windows(app=None):
    """On-screen normal windows (window_id, owner, pid, title, x/y/w/h)."""
    return _call("list_windows", app=app)["windows"]


def frontmost_app():
    return _call("frontmost_app")["app"]


def permissions():
    return _call("permissions")


# === NON-DISTURBING: target-app control in the background ==================

def get_app_state(app, screenshot=True, max_elements=400, interactive_only=False,
                  diff=False, annotate=True):
    """Snapshot an app's frontmost window WITHOUT raising it or stealing focus.

    Returns {app, pid, window, window_bounds, elements:[...], text, screenshot}.
    Read `text` (the numbered tree) to choose an element. Each element has a
    cross-platform `kind` ("button", "text", "checkbox", ...) next to its native
    `role`. The screenshot is a background capture of just that window;
    screenshot["annotated_path"] is the same image with every element's box and
    index drawn on it. Caches this snapshot so click/set_value/perform_action
    resolve against it.

    interactive_only=True returns only elements with actions or a settable value
    (indices unchanged). diff=True adds {"added", "removed"} tree lines compared
    with the previous snapshot of this app.
    """
    return _call("get_app_state", app=app, screenshot=screenshot, max_elements=max_elements,
                 interactive_only=interactive_only, diff=diff, annotate=annotate)


def find(app, label=None, role=None):
    """Elements in the last get_app_state(app) matching label (case-insensitive,
    exact first, then substring) and role (a kind like "button" or a native role)."""
    return _call("find", app=app, label=label, role=role)["elements"]


def click(app, element_index=None, method="ax", button="left", count=1, label=None, role=None):
    """Activate an element from the last get_app_state(app). NON-DISTURBING.

    Name the element by element_index, or by label= (and role= to disambiguate).
    method="ax" (default): the accessibility press action in the background, no
    cursor move, no focus change. method="foreground": a real click instead.
    """
    return _call("click", app=app, element_index=element_index, method=method, button=button,
                 count=count, label=label, role=role)


def set_value(app, element_index=None, value=None, label=None, role=None):
    """Set a text element's value directly via accessibility. NON-DISTURBING.

    No keystrokes, works on a background window. Use for text fields / areas
    (element must be `settable` in get_app_state's tree).
    """
    if value is None:
        raise ValueError("pass value=")
    return _call("set_value", app=app, element_index=element_index, value=value,
                 label=label, role=role)


def perform_action(app, element_index=None, action="AXPress", label=None, role=None):
    """Invoke an accessibility action (AXPress, AXShowMenu, AXIncrement, ...;
    on Windows also Invoke, Toggle, Select, Expand, Collapse). NON-DISTURBING."""
    return _call("perform_action", app=app, element_index=element_index, action=action,
                 label=label, role=role)


def wait_for(app, label=None, role=None, timeout=10.0, interval=0.5):
    """Re-snapshot app until an element matches label/role. Returns the first
    match; raises TimeoutError after `timeout` seconds."""
    import time as _t
    deadline = _t.time() + timeout
    while True:
        get_app_state(app, screenshot=False)
        hits = find(app, label=label, role=role)
        if hits:
            return hits[0]
        if _t.time() >= deadline:
            raise TimeoutError(f"no element label={label!r} role={role!r} in {app!r} after {timeout}s")
        _t.sleep(interval)


def wait_window(title, timeout=10.0, interval=0.5):
    """Wait until an on-screen window's title contains `title`; returns it."""
    import time as _t
    deadline = _t.time() + timeout
    while True:
        for w in list_windows():
            if title.lower() in w["title"].lower():
                return w
        if _t.time() >= deadline:
            raise TimeoutError(f"no window titled {title!r} after {timeout}s")
        _t.sleep(interval)


def capture_window(app=None, window_id=None, path=None):
    """Background capture of a single window (occluded/not-frontmost is fine).

    Pass window_id (from list_windows) or app (captures its largest window).
    Returns {path, width, height}. Does not touch the user's foreground.
    """
    if window_id is None:
        if app is None:
            raise ValueError("pass app= or window_id=")
        wins = list_windows(app=app)
        if not wins:
            raise ValueError(f"no on-screen window for {app!r}")
        window_id = max(wins, key=lambda w: w["w"] * w["h"])["window_id"]
    return _call("capture_window", window_id=window_id, path=path)


# === FOREGROUND: global input (moves the cursor, interrupts the user) ======

def capture_screenshot(path=None, region=None, data_url=False):
    """Full-screen (or region=(x,y,w,h)) capture in logical points, 1:1 with clicks."""
    return _call("capture_screenshot", path=path, region=region, data_url=data_url)


def activate_app(app):
    """Bring an app to the front (name / bundle id / pid). Changes focus."""
    return _call("activate_app", app=app)


# --- screen video recording (passive; does not disturb the user) ----------

def record_screen(path=None, seconds=5, display=1, region=None, show_clicks=False, audio=False):
    """Record the desktop to an .mov for a fixed duration, returning the finished file.

    Passive capture — no input, no focus change. Blocks the caller (not the
    daemon) for `seconds`, then returns {path, bytes}.
    """
    import time as _t
    r = _call("record_start", name="_fixed", path=path, seconds=seconds, display=display,
              region=region, show_clicks=show_clicks, audio=audio)
    _t.sleep(float(seconds) + 1.0)  # let screencapture finalize
    st = recording_status().get("_fixed", {})
    p = r["path"]
    import os as _os
    return {"path": p, "bytes": _os.path.getsize(p) if _os.path.exists(p) else 0,
            "running": st.get("running", False)}


def start_recording(path=None, name="screen", display=1, region=None, show_clicks=False, audio=False):
    """Begin an open-ended desktop recording. Returns {name, path, pid}.

    Do work, then stop_recording(name) to finalize. Passive; does not disturb.
    """
    return _call("record_start", name=name, path=path, display=display,
                 region=region, show_clicks=show_clicks, audio=audio)


def stop_recording(name="screen"):
    """Finalize an open-ended recording. Returns {name, path, bytes}."""
    return _call("record_stop", name=name)


def recording_status():
    """Currently active recordings: {name: {path, running}}."""
    return _call("record_status")["recordings"]


def click_at_xy(x, y, button="left", count=1):
    """FOREGROUND global click at a screen coordinate (moves the real cursor)."""
    return _call("click", x=x, y=y, button=button, count=count)


def double_click_at_xy(x, y, button="left"):
    return _call("click", x=x, y=y, button=button, count=2)


def right_click_at_xy(x, y):
    return _call("click", x=x, y=y, button="right", count=1)


def move_mouse(x, y):
    return _call("move_mouse", x=x, y=y)


def drag(from_x, from_y, to_x, to_y, button="left", steps=25):
    """FOREGROUND drag between screen coordinates."""
    return _call("drag", from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y, button=button, steps=steps)


def scroll(dx=0, dy=0):
    """FOREGROUND scroll (line units). Positive dy up, negative down."""
    return _call("scroll", dx=dx, dy=dy)


def type_text(text, per_char_delay=0.0):
    """FOREGROUND: type unicode into whatever is focused. Prefer set_value when possible."""
    return _call("type_text", text=text, per_char_delay=per_char_delay)


def press_key(key):
    """FOREGROUND key chord, e.g. 'cmd+c', 'cmd+shift+t', 'Return', 'Escape', 'down'."""
    return _call("press_key", key=key)


__all__ = [
    "screen_info", "list_apps", "list_windows", "frontmost_app", "permissions",
    "get_app_state", "find", "click", "set_value", "perform_action", "capture_window",
    "wait_for", "wait_window",
    "capture_screenshot", "activate_app",
    "record_screen", "start_recording", "stop_recording", "recording_status",
    "click_at_xy", "double_click_at_xy", "right_click_at_xy",
    "move_mouse", "drag", "scroll", "type_text", "press_key",
]

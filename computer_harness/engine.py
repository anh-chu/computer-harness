"""Native macOS computer-use engine.

Input synthesis via CoreGraphics CGEvent, pixels via `screencapture` +
CGWindowList, and the accessibility tree via AXUIElement. This module runs
inside the daemon (the process that holds the Accessibility + Screen Recording
TCC grants). Everything here works in *logical* points: screenshots are
downscaled to the display's logical resolution so 1 screenshot pixel == 1 click
point, exactly like a browser CSS pixel. That is what makes "read the pixel off
the image, click_at_xy there" work without the agent ever thinking about Retina
scale factors.
"""
import base64
import os
import signal
import subprocess
import time

import Quartz
from AppKit import NSWorkspace, NSRunningApplication, NSBitmapImageRep, NSPNGFileType
import ApplicationServices as AX

# ---------------------------------------------------------------------------
# Coordinate space
# ---------------------------------------------------------------------------
# CGEvent posts in global points (top-left origin). AXPosition returns global
# points (top-left origin). screencapture writes physical pixels (2x on Retina).
# We resize screenshots down to logical points so all three share one space.

def main_display():
    return Quartz.CGMainDisplayID()


def logical_size():
    """(width, height) of the main display in points."""
    b = Quartz.CGDisplayBounds(main_display())
    return int(b.size.width), int(b.size.height)


def backing_scale():
    did = main_display()
    px_w = Quartz.CGDisplayPixelsWide(did)
    lg_w, _ = logical_size()
    return (px_w / lg_w) if lg_w else 1.0


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------
_BTN = {
    "left": Quartz.kCGMouseButtonLeft,
    "right": Quartz.kCGMouseButtonRight,
    "middle": Quartz.kCGMouseButtonCenter,
    "l": Quartz.kCGMouseButtonLeft,
    "r": Quartz.kCGMouseButtonRight,
    "m": Quartz.kCGMouseButtonCenter,
}
_DOWN = {
    "left": Quartz.kCGEventLeftMouseDown,
    "right": Quartz.kCGEventRightMouseDown,
    "middle": Quartz.kCGEventOtherMouseDown,
}
_UP = {
    "left": Quartz.kCGEventLeftMouseUp,
    "right": Quartz.kCGEventRightMouseUp,
    "middle": Quartz.kCGEventOtherMouseUp,
}
_DRAG = {
    "left": Quartz.kCGEventLeftMouseDragged,
    "right": Quartz.kCGEventRightMouseDragged,
    "middle": Quartz.kCGEventOtherMouseDragged,
}


def _norm_button(b):
    b = (b or "left").lower()
    if b in ("l", "left"):
        return "left"
    if b in ("r", "right"):
        return "right"
    if b in ("m", "middle"):
        return "middle"
    return "left"


def _post(ev):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def move_mouse(x, y):
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0))


def click(x, y, button="left", count=1):
    button = _norm_button(button)
    btn = _BTN[button]
    move_mouse(x, y)
    for i in range(1, int(count) + 1):
        for t in (_DOWN[button], _UP[button]):
            ev = Quartz.CGEventCreateMouseEvent(None, t, (x, y), btn)
            # click-state field drives double/triple-click recognition
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
            _post(ev)
            time.sleep(0.01)


def drag(from_x, from_y, to_x, to_y, button="left", steps=25, hold=0.02):
    button = _norm_button(button)
    btn = _BTN[button]
    move_mouse(from_x, from_y)
    _post(Quartz.CGEventCreateMouseEvent(None, _DOWN[button], (from_x, from_y), btn))
    time.sleep(hold)
    steps = max(1, int(steps))
    for i in range(1, steps + 1):
        x = from_x + (to_x - from_x) * i / steps
        y = from_y + (to_y - from_y) * i / steps
        _post(Quartz.CGEventCreateMouseEvent(None, _DRAG[button], (x, y), btn))
        time.sleep(hold)
    _post(Quartz.CGEventCreateMouseEvent(None, _UP[button], (to_x, to_y), btn))


def scroll(dx=0, dy=0):
    """Positive dy scrolls up, negative down (line units)."""
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitLine, 2, int(dy), int(dx)
    )
    _post(ev)


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
# US-layout virtual keycodes. type_text() bypasses this via UnicodeString so it
# can emit any character; the map is only for press_key() chords / named keys.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
    "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "return": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50,
    "delete": 51, "escape": 53, "forwarddelete": 117,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_KEY_ALIASES = {
    "enter": "return", "esc": "escape", "backspace": "delete",
    "del": "forwarddelete", "pgup": "pageup", "pgdn": "pagedown",
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
    "spacebar": "space", "cmd": "command", "opt": "option", "ctrl": "control",
    # X-keysym style names accepted for parity with sky
    "control_l": "control", "control_r": "control", "shift_l": "shift",
    "shift_r": "shift", "alt_l": "option", "alt_r": "option", "super_l": "command",
    "super_r": "command", "meta_l": "command", "meta_r": "command",
}
_FLAGS = {
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


def type_text(text, per_char_delay=0.0):
    for ch in text:
        for keydown in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, keydown)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            _post(ev)
        if per_char_delay:
            time.sleep(per_char_delay)


def press_key(chord):
    """Press a `+`-separated chord, e.g. 'cmd+shift+t', 'Return', 'Control_L+a'."""
    parts = [p.strip().lower() for p in str(chord).split("+") if p.strip()]
    flags = 0
    key = None
    for p in parts:
        p = _KEY_ALIASES.get(p, p)
        if p in ("command", "shift", "option", "alt", "control", "fn"):
            flags |= _FLAGS[p]
        else:
            key = p
    if key is None:
        raise ValueError(f"no non-modifier key in chord {chord!r}")
    key = _KEY_ALIASES.get(key, key)
    kc = _KEYCODES.get(key)
    if kc is None:
        raise ValueError(f"unknown key {key!r} in chord {chord!r}")
    down = Quartz.CGEventCreateKeyboardEvent(None, kc, True)
    Quartz.CGEventSetFlags(down, flags)
    _post(down)
    time.sleep(0.01)
    up = Quartz.CGEventCreateKeyboardEvent(None, kc, False)
    Quartz.CGEventSetFlags(up, flags)
    _post(up)


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def _resize_png_to_logical(path, logical_w, logical_h):
    """Downscale a Retina screenshot in place to logical points via `sips`.

    Keeps 1 screenshot pixel == 1 click point. `sips` ships with macOS so we
    avoid a Pillow hard-dependency for the common full-screen path.
    """
    try:
        subprocess.run(
            ["/usr/bin/sips", "-z", str(logical_h), str(logical_w), os.path.abspath(path)],
            check=True, capture_output=True,
        )
    except Exception:
        pass  # fall back to physical-pixel image; caller still gets a valid PNG


def capture_screenshot(path, region=None, resize_logical=True):
    """Full-screen (or region) capture. region = (x, y, w, h) in points.

    Returns dict {path, width, height, scale}. width/height are the image's
    final pixel dims, which equal points when resize_logical is on.
    """
    path = os.path.abspath(path)  # never let a "-"-leading path parse as a flag
    cmd = ["/usr/sbin/screencapture", "-x", "-t", "png"]
    lg_w, lg_h = logical_size()
    if region:
        rx, ry, rw, rh = region
        scale = backing_scale()
        # -R takes physical pixels for the crop on Retina displays
        cmd += ["-R", f"{rx*scale:.0f},{ry*scale:.0f},{rw*scale:.0f},{rh*scale:.0f}"]
        target_w, target_h = int(rw), int(rh)
    else:
        target_w, target_h = lg_w, lg_h
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)
    if resize_logical:
        _resize_png_to_logical(path, target_w, target_h)
    return {"path": path, "width": target_w, "height": target_h, "scale": backing_scale()}


def screenshot_data_url(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


# ---------------------------------------------------------------------------
# Screen video recording (screencapture -v). Records the display passively —
# no input, no focus change — so it never disturbs the user.
# ---------------------------------------------------------------------------
_RECORDERS = {}  # name -> {"popen": Popen, "path": str}


def _rec_cmd(path, seconds=None, region=None, display=1, show_clicks=False, audio=False):
    path = os.path.abspath(path)
    cmd = ["/usr/sbin/screencapture", "-v", "-x", f"-D{int(display)}"]
    if seconds:
        cmd.append(f"-V{int(seconds)}")
    if show_clicks:
        cmd.append("-k")
    if audio:
        cmd.append("-g")
    if region:
        rx, ry, rw, rh = region
        cmd += ["-R", f"{rx:.0f},{ry:.0f},{rw:.0f},{rh:.0f}"]
    cmd.append(path)
    return cmd


def start_recording(name, path, seconds=None, region=None, display=1, show_clicks=False, audio=False):
    rec = _RECORDERS.get(name)
    if rec and rec["popen"].poll() is None:
        raise ValueError(f"recording {name!r} already running -> {rec['path']}")
    path = os.path.abspath(path)
    p = subprocess.Popen(_rec_cmd(path, seconds, region, display, show_clicks, audio))
    _RECORDERS[name] = {"popen": p, "path": path}
    return {"name": name, "path": path, "pid": p.pid, "seconds": seconds}


def stop_recording(name, timeout=12):
    rec = _RECORDERS.get(name)
    if not rec:
        raise ValueError(f"no recording named {name!r}")
    p = rec["popen"]
    if p.poll() is None:
        p.send_signal(signal.SIGINT)  # finalizes the .mov
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.terminate()
    _RECORDERS.pop(name, None)
    path = rec["path"]
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {"name": name, "path": path, "bytes": size}


def recording_status():
    return {
        n: {"path": r["path"], "running": r["popen"].poll() is None}
        for n, r in _RECORDERS.items()
    }


def stop_all_recordings():
    """Finalize any in-flight recordings — called on daemon shutdown so a
    `screencapture -v` child isn't orphaned and left recording."""
    for name in list(_RECORDERS):
        try:
            stop_recording(name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows (background-capable capture — the non-disturbance foundation)
# ---------------------------------------------------------------------------
# CGWindowListCreateImage reads a window's backing store even when it is
# occluded or not frontmost, so we can SEE any app without raising it or
# touching the user's foreground.

def list_windows(pid=None):
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    raw = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    out = []
    for w in raw:
        if w.get("kCGWindowLayer", 0) != 0:  # 0 == normal app window layer
            continue
        b = w.get("kCGWindowBounds", {})
        rec = {
            "window_id": int(w.get("kCGWindowNumber", 0)),
            "owner": str(w.get("kCGWindowOwnerName", "")),
            "pid": int(w.get("kCGWindowOwnerPID", 0)),
            "title": str(w.get("kCGWindowName", "") or ""),
            "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
            "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0)),
        }
        if pid is not None and rec["pid"] != int(pid):
            continue
        out.append(rec)
    return out


def _best_window_for_pid(pid):
    wins = [w for w in list_windows(pid) if w["w"] > 60 and w["h"] > 60]
    if not wins:
        return None
    # Frontmost app's on-screen list is ordered front-to-back, so first is topmost.
    return max(wins, key=lambda w: w["w"] * w["h"])


def capture_window(window_id, path):
    path = os.path.abspath(path)
    img = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        int(window_id),
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
    )
    if img is None:
        return None
    rep = NSBitmapImageRep.alloc().initWithCGImage_(img)
    data = rep.representationUsingType_properties_(NSPNGFileType, None)
    data.writeToFile_atomically_(path, True)
    return {"path": path, "width": Quartz.CGImageGetWidth(img), "height": Quartz.CGImageGetHeight(img)}


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

def list_apps():
    out = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        # 0 == NSApplicationActivationPolicyRegular (has a Dock icon / UI)
        if app.activationPolicy() != 0:
            continue
        out.append({
            "name": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or ""),
            "pid": int(app.processIdentifier()),
            "active": bool(app.isActive()),
        })
    out.sort(key=lambda a: (not a["active"], a["name"].lower()))
    return out


def frontmost_app():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if not app:
        return None
    return {
        "name": str(app.localizedName() or ""),
        "bundle_id": str(app.bundleIdentifier() or ""),
        "pid": int(app.processIdentifier()),
        "active": True,
    }


def _resolve_app(identifier):
    """Return NSRunningApplication for a name / bundle id / pid, else None."""
    ident = str(identifier)
    ws = NSWorkspace.sharedWorkspace()
    apps = list(ws.runningApplications())
    if ident.isdigit():
        pid = int(ident)
        for a in apps:
            if int(a.processIdentifier()) == pid:
                return a
    for a in apps:
        if str(a.bundleIdentifier() or "") == ident:
            return a
    low = ident.lower()
    for a in apps:
        if str(a.localizedName() or "").lower() == low:
            return a
    for a in apps:
        if low in str(a.localizedName() or "").lower():
            return a
    return None


def activate_app(identifier):
    app = _resolve_app(identifier)
    if not app:
        raise ValueError(f"app not found: {identifier!r}")
    # 1 << 1 == NSApplicationActivateIgnoringOtherApps
    app.activateWithOptions_(1 << 1)
    time.sleep(0.3)
    return {"name": str(app.localizedName() or ""), "pid": int(app.processIdentifier())}


# ---------------------------------------------------------------------------
# Accessibility tree
# ---------------------------------------------------------------------------
# Actionable roles we surface with an element_index. Everything else is walked
# for structure but only emitted when it carries a title/value worth showing.
_ACTIONABLE = {
    "AXButton", "AXMenuItem", "AXMenuButton", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXLink", "AXTextField", "AXTextArea", "AXComboBox",
    "AXSlider", "AXTab", "AXTabGroup", "AXDisclosureTriangle", "AXCell",
    "AXRow", "AXColumn", "AXToolbar", "AXSearchField", "AXStepper",
    "AXSegmentedControl", "AXIncrementor",
}


def _ax_copy(el, attr):
    err, val = AX.AXUIElementCopyAttributeValue(el, attr, None)
    if err != 0:
        return None
    return val


def _ax_point(el, attr):
    val = _ax_copy(el, attr)
    if val is None:
        return None
    ok, pt = AX.AXValueGetValue(val, AX.kAXValueCGPointType, None)
    if ok:
        return (pt.x, pt.y)
    return None


def _ax_size(el, attr):
    val = _ax_copy(el, attr)
    if val is None:
        return None
    ok, sz = AX.AXValueGetValue(val, AX.kAXValueCGSizeType, None)
    if ok:
        return (sz.width, sz.height)
    return None


def _str(v):
    if v is None:
        return ""
    try:
        return str(v)
    except Exception:
        return ""


def get_app_state(identifier, max_elements=400, capture=True):
    """Snapshot an app's frontmost window WITHOUT raising it or moving focus.

    Captures the window via CGWindowListCreateImage (works on a backgrounded /
    occluded window) and walks its accessibility tree. Each element carries both
    global-screen coordinates (for the CGEvent foreground fallback) and
    window-relative coordinates `rel` (which map 1:1 to the returned screenshot).

    Returns {app, pid, window, window_bounds, elements:[...], text, screenshot}.
    """
    app = _resolve_app(identifier)
    if not app:
        raise ValueError(f"app not found: {identifier!r}")
    pid = int(app.processIdentifier())
    ax_app = AX.AXUIElementCreateApplication(pid)

    window = _ax_copy(ax_app, AX.kAXFocusedWindowAttribute)
    if window is None:
        wins = _ax_copy(ax_app, AX.kAXWindowsAttribute)
        window = wins[0] if wins else None
    root = window or ax_app
    win_title = _str(_ax_copy(window, AX.kAXTitleAttribute)) if window else ""

    cg_win = _best_window_for_pid(pid)
    win_x = cg_win["x"] if cg_win else 0.0
    win_y = cg_win["y"] if cg_win else 0.0

    elements = []
    lines = []

    def walk(el, depth):
        if len(elements) >= max_elements:
            return
        role = _str(_ax_copy(el, AX.kAXRoleAttribute))
        title = _str(_ax_copy(el, AX.kAXTitleAttribute))
        desc = _str(_ax_copy(el, AX.kAXDescriptionAttribute))
        value = _str(_ax_copy(el, AX.kAXValueAttribute))
        label = title or desc or _str(_ax_copy(el, "AXLabel"))
        pos = _ax_point(el, AX.kAXPositionAttribute)
        size = _ax_size(el, AX.kAXSizeAttribute)

        emit = role in _ACTIONABLE or (label and pos and size)
        if emit and pos and size and size[0] > 0 and size[1] > 0:
            idx = len(elements)
            cx = pos[0] + size[0] / 2
            cy = pos[1] + size[1] / 2
            aerr, acts = AX.AXUIElementCopyActionNames(el, None)
            actions = [str(a) for a in acts] if aerr == 0 and acts else []
            rec = {
                "index": idx, "role": role, "label": label[:120], "value": value[:160],
                "x": round(pos[0], 1), "y": round(pos[1], 1),
                "w": round(size[0], 1), "h": round(size[1], 1),
                "center": [round(cx, 1), round(cy, 1)],
                "rel": [round(cx - win_x, 1), round(cy - win_y, 1)],
                "actions": actions,
                "settable": bool(_ax_settable(el, AX.kAXValueAttribute)),
            }
            elements.append(rec)
            disp = f"[{idx}] {role}"
            if label:
                disp += f" “{label[:80]}”"
            if value and value != label:
                disp += f" = {value[:60]}"
            if "AXPress" in actions:
                disp += "  • pressable"
            elif rec["settable"]:
                disp += "  • settable"
            lines.append("  " * min(depth, 8) + disp)

        for c in (_ax_copy(el, AX.kAXChildrenAttribute) or []):
            walk(c, depth + 1)

    if root is not None:
        walk(root, 0)

    state = {
        "app": str(app.localizedName() or ""),
        "pid": pid,
        "window": win_title,
        "window_bounds": cg_win,
        "elements": elements,
        "text": "\n".join(lines),
        "screenshot": None,
    }
    if capture and cg_win:
        import time as _t
        path = str(_screenshot_dir() / f"state-{int(_t.time()*1000)}.png")
        shot = capture_window(cg_win["window_id"], path)
        if shot:
            _resize_png_to_logical(shot["path"], int(cg_win["w"]), int(cg_win["h"]))
            shot["width"] = int(cg_win["w"])
            shot["height"] = int(cg_win["h"])
        state["screenshot"] = shot
    return state


def _screenshot_dir():
    from . import ipc
    return ipc.screenshot_dir()


def _ax_settable(el, attr):
    err, settable = AX.AXUIElementIsAttributeSettable(el, attr, None)
    return err == 0 and bool(settable)


def _locate_element(pid, rec):
    """Re-find the live AXUIElement matching a cached record, by frame + role."""
    ax_app = AX.AXUIElementCreateApplication(pid)
    match = {"el": None}

    def find(el):
        if match["el"] is not None:
            return
        pos = _ax_point(el, AX.kAXPositionAttribute)
        size = _ax_size(el, AX.kAXSizeAttribute)
        if (pos and size
                and abs(pos[0] - rec["x"]) < 2 and abs(pos[1] - rec["y"]) < 2
                and abs(size[0] - rec["w"]) < 2 and abs(size[1] - rec["h"]) < 2):
            role = _str(_ax_copy(el, AX.kAXRoleAttribute))
            if role == rec["role"]:
                match["el"] = el
                return
        for c in (_ax_copy(el, AX.kAXChildrenAttribute) or []):
            find(c)

    find(ax_app)
    return match["el"]


def _rec_for_index(state_elements, element_index):
    for e in state_elements:
        if e["index"] == element_index:
            return e
    raise ValueError(f"element_index {element_index} not in last app state")


def click_element(pid, state_elements, element_index, button="left", count=1, method="ax"):
    """Activate an indexed element.

    method="ax" (default, NON-DISTURBING): AXPress the element — no cursor move,
    no focus steal, works on a background window. Falls back to a pid-targeted
    CGEvent click if the element exposes no AXPress action.
    method="foreground": global CGEvent click (moves the cursor, needs the app
    frontmost) — for canvas apps / games with no accessibility actions.
    """
    rec = _rec_for_index(state_elements, element_index)
    if method == "ax" and "AXPress" in rec.get("actions", []):
        el = _locate_element(pid, rec)
        if el is not None:
            err = AX.AXUIElementPerformAction(el, "AXPress")
            if err == 0:
                return {"clicked": element_index, "via": "AXPress",
                        "role": rec["role"], "label": rec["label"]}
    cx, cy = rec["center"]
    if method == "ax":
        post_click_to_pid(pid, cx, cy, button=button, count=count)
        return {"clicked": element_index, "via": "CGEventPostToPid", "at": [cx, cy],
                "role": rec["role"], "label": rec["label"]}
    click(cx, cy, button=button, count=count)
    return {"clicked": element_index, "via": "foreground", "at": [cx, cy],
            "role": rec["role"], "label": rec["label"]}


def set_element_value(pid, state_elements, element_index, value):
    """Set a text element's value directly via AX. Non-disturbing; no keystrokes."""
    rec = _rec_for_index(state_elements, element_index)
    el = _locate_element(pid, rec)
    if el is None:
        raise ValueError("could not relocate element to set value")
    err = AX.AXUIElementSetAttributeValue(el, AX.kAXValueAttribute, value)
    if err != 0:
        raise RuntimeError(f"AXUIElementSetAttributeValue failed: {err}")
    return {"set_value": element_index, "chars": len(value)}


def perform_action(pid, state_elements, element_index, action):
    """Invoke an arbitrary AX action (e.g. AXPress, AXShowMenu) on an element."""
    rec = _rec_for_index(state_elements, element_index)
    el = _locate_element(pid, rec)
    if el is None:
        raise ValueError("could not relocate element for action")
    err = AX.AXUIElementPerformAction(el, action)
    if err != 0:
        raise RuntimeError(f"AXUIElementPerformAction failed: {err}")
    return {"performed": action, "element_index": element_index}


def post_click_to_pid(pid, x, y, button="left", count=1):
    """CGEvent click delivered to a specific app's queue (no global cursor move)."""
    button = _norm_button(button)
    btn = _BTN[button]
    for i in range(1, int(count) + 1):
        for t in (_DOWN[button], _UP[button]):
            ev = Quartz.CGEventCreateMouseEvent(None, t, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventPostToPid(int(pid), ev)
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Permission probes
# ---------------------------------------------------------------------------

def has_accessibility():
    return bool(AX.AXIsProcessTrusted())


def has_screen_recording():
    """Best-effort probe: try a 1x1 capture to a temp file."""
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "ch_screencap_probe.png")
    try:
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-R", "0,0,1,1", "-t", "png", p],
            check=True, capture_output=True, timeout=5,
        )
        ok = os.path.exists(p) and os.path.getsize(p) > 0
        try:
            os.remove(p)
        except OSError:
            pass
        return ok
    except Exception:
        return False

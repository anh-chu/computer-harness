"""Native Windows computer-use engine. Same function surface as engine.py.

Input synthesis via SendInput, pixels via PrintWindow (one window, works while
occluded) and Pillow ImageGrab (full screen), and the accessibility tree via UI
Automation. The process is per-monitor DPI aware (set by `uiautomation` on
import), so every coordinate here is a physical pixel: UIA bounding rects,
GetWindowRect, SetCursorPos and screenshots all share one space, and 1
screenshot pixel == 1 click point, like engine.py.

Limits vs macOS:
  - PrintWindow cannot capture a minimized window.
  - The background click fallback (PostMessage) reaches classic Win32 controls
    only; Chromium/Electron/UWP ignore it. Use method="foreground" for those.
  - A non-elevated daemon cannot read or drive windows of elevated (admin)
    apps (UIPI). Run the daemon elevated to reach them.
  - Nothing works on the secure desktop (lock screen, UAC prompt) or without an
    interactive session (services, SSH sessions).
"""
import base64
import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes

import psutil
import uiautomation as auto  # sets per-monitor DPI awareness on import
from PIL import Image, ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, ctypes.c_uint]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

# ---------------------------------------------------------------------------
# Coordinate space
# ---------------------------------------------------------------------------

def logical_size():
    """(width, height) of the primary display in pixels (the click space)."""
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def backing_scale():
    """Primary display scale factor (1.0 == 96 DPI). Informational only."""
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# SendInput
# ---------------------------------------------------------------------------
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
_MOUSEEVENTF_WHEEL, _MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
_WHEEL_DELTA = 120
_MOUSE_FLAGS = {  # button -> (down, up)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*inputs):
    arr = (_INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if n != len(inputs):
        raise OSError(f"SendInput blocked ({ctypes.get_last_error()}): secure desktop or an elevated window in front?")


def _mouse(flags, data=0):
    return _INPUT(_INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, 0)))


def _key(vk=0, scan=0, flags=0):
    return _INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------

def _norm_button(b):
    b = (b or "left").lower()
    if b in ("l", "left"):
        return "left"
    if b in ("r", "right"):
        return "right"
    if b in ("m", "middle"):
        return "middle"
    return "left"


def move_mouse(x, y):
    user32.SetCursorPos(int(round(x)), int(round(y)))


def click(x, y, button="left", count=1):
    down, up = _MOUSE_FLAGS[_norm_button(button)]
    move_mouse(x, y)
    for _ in range(int(count)):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.01)  # well under the double-click time


def drag(from_x, from_y, to_x, to_y, button="left", steps=25, hold=0.02):
    down, up = _MOUSE_FLAGS[_norm_button(button)]
    move_mouse(from_x, from_y)
    _send(_mouse(down))
    time.sleep(hold)
    steps = max(1, int(steps))
    for i in range(1, steps + 1):
        move_mouse(from_x + (to_x - from_x) * i / steps, from_y + (to_y - from_y) * i / steps)
        time.sleep(hold)
    _send(_mouse(up))


def scroll(dx=0, dy=0):
    """Positive dy scrolls up, negative down (line units)."""
    if dy:
        _send(_mouse(_MOUSEEVENTF_WHEEL, int(dy) * _WHEEL_DELTA))
    if dx:
        _send(_mouse(_MOUSEEVENTF_HWHEEL, int(dx) * _WHEEL_DELTA))


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
# Virtual-key codes. type_text() bypasses this via KEYEVENTF_UNICODE so it can
# emit any character; the map is only for press_key() chords / named keys.
_VK = {
    "return": 0x0D, "tab": 0x09, "space": 0x20, "delete": 0x08, "escape": 0x1B,
    "forwarddelete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
_VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
_VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D, 0x2C}
_MODS = {"control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
_KEY_ALIASES = {
    "enter": "return", "esc": "escape", "backspace": "delete",
    "del": "forwarddelete", "pgup": "pageup", "pgdn": "pagedown",
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
    "spacebar": "space", "ctrl": "control", "option": "alt", "opt": "alt",
    # macOS habit: cmd+c means ctrl+c here. Use "win" for the Windows key.
    "command": "control", "cmd": "control",
    "super": "win", "meta": "win", "windows": "win",
    "control_l": "control", "control_r": "control", "shift_l": "shift",
    "shift_r": "shift", "alt_l": "alt", "alt_r": "alt", "super_l": "win",
    "super_r": "win", "meta_l": "win", "meta_r": "win",
}


def type_text(text, per_char_delay=0.0):
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):  # one UTF-16 unit at a time (surrogates included)
        unit = int.from_bytes(data[i:i + 2], "little")
        _send(_key(scan=unit, flags=_KEYEVENTF_UNICODE),
              _key(scan=unit, flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
        if per_char_delay:
            time.sleep(per_char_delay)


def press_key(chord):
    """Press a `+`-separated chord, e.g. 'ctrl+shift+t', 'Return', 'cmd+a' (= ctrl+a)."""
    parts = [p.strip().lower() for p in str(chord).split("+") if p.strip()]
    mods, key = [], None
    for p in parts:
        p = _KEY_ALIASES.get(p, p)
        if p in _MODS:
            mods.append(_MODS[p])
        else:
            key = p
    if key is None:
        raise ValueError(f"no non-modifier key in chord {chord!r}")
    vk = _VK.get(key)
    if vk is None:
        raise ValueError(f"unknown key {key!r} in chord {chord!r}")

    def ev(code, up):
        flags = (_KEYEVENTF_EXTENDEDKEY if code in _EXTENDED else 0) | (_KEYEVENTF_KEYUP if up else 0)
        return _key(vk=code, flags=flags)

    _send(*[ev(m, False) for m in mods], ev(vk, False))
    time.sleep(0.01)
    _send(ev(vk, True), *[ev(m, True) for m in reversed(mods)])


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def capture_screenshot(path, region=None, resize_logical=True):
    """Primary-screen (or region) capture. region = (x, y, w, h) in pixels.

    resize_logical is accepted for parity; screenshots are already 1:1 with
    click coordinates on Windows.
    """
    path = os.path.abspath(path)
    w, h = logical_size()
    bbox = (0, 0, w, h)
    if region:
        rx, ry, rw, rh = (int(round(v)) for v in region)
        bbox = (rx, ry, rx + rw, ry + rh)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(path, "PNG")
    return {"path": path, "width": img.width, "height": img.height, "scale": backing_scale()}


def screenshot_data_url(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


# ---------------------------------------------------------------------------
# Screen video recording via ffmpeg gdigrab (Windows has no built-in CLI
# recorder). Passive: no input, no focus change.
# ---------------------------------------------------------------------------
REC_EXT = ".mp4"
_RECORDERS = {}  # name -> {"popen": Popen, "path": str}


def _rec_cmd(path, seconds=None, region=None):
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("recording needs ffmpeg on PATH (winget install Gyan.FFmpeg)")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", "30"]
    if region:
        rx, ry, rw, rh = (int(round(v)) for v in region)
        rw, rh = rw - rw % 2, rh - rh % 2  # yuv420p needs even dimensions
        cmd += ["-offset_x", str(rx), "-offset_y", str(ry), "-video_size", f"{rw}x{rh}"]
    cmd += ["-i", "desktop"]
    if seconds:
        cmd += ["-t", str(int(seconds))]
    cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", path]
    return cmd


def start_recording(name, path, seconds=None, region=None, display=1, show_clicks=False, audio=False):
    """display / show_clicks are ignored on Windows: gdigrab records the whole
    virtual desktop (or `region`) and always draws the cursor."""
    if audio:
        raise ValueError("audio recording is not supported on Windows")
    rec = _RECORDERS.get(name)
    if rec and rec["popen"].poll() is None:
        raise ValueError(f"recording {name!r} already running -> {rec['path']}")
    path = os.path.abspath(path)
    p = subprocess.Popen(_rec_cmd(path, seconds, region), stdin=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    _RECORDERS[name] = {"popen": p, "path": path}
    return {"name": name, "path": path, "pid": p.pid, "seconds": seconds}


def stop_recording(name, timeout=12):
    rec = _RECORDERS.get(name)
    if not rec:
        raise ValueError(f"no recording named {name!r}")
    p = rec["popen"]
    if p.poll() is None:
        try:
            p.communicate(b"q", timeout=timeout)  # "q" makes ffmpeg finalize the file
        except subprocess.TimeoutExpired:
            p.kill()
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
    for name in list(_RECORDERS):
        try:
            stop_recording(name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows (window_id == HWND)
# ---------------------------------------------------------------------------
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _proc_name(pid):
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return ""


def _proc_path(pid):
    try:
        return psutil.Process(pid).exe()
    except (psutil.Error, OSError):
        return ""


def _rect(hwnd):
    """Visible frame (no drop shadow), falling back to GetWindowRect."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _is_app_window(hwnd):
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    if user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
        return False
    cloaked = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return not cloaked.value  # cloaked == on another virtual desktop / suspended UWP


def _enum_windows():
    """Visible top-level app windows, front-to-back (EnumWindows is z-ordered)."""
    out = []

    def cb(hwnd, _):
        if _is_app_window(hwnd):
            out.append(hwnd)
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def list_windows(pid=None):
    out = []
    names = {}
    for hwnd in _enum_windows():
        wpid = _pid_of(hwnd)
        if pid is not None and wpid != int(pid):
            continue
        if wpid not in names:
            names[wpid] = _proc_name(wpid)
        x, y, w, h = _rect(hwnd)
        out.append({
            "window_id": int(hwnd), "owner": names[wpid], "pid": wpid, "title": _title(hwnd),
            "x": float(x), "y": float(y), "w": float(w), "h": float(h),
        })
    return out


def capture_window(window_id, path):
    """PrintWindow(PW_RENDERFULLCONTENT): renders the window even when covered
    by other windows; does not raise it. Fails on minimized windows."""
    path = os.path.abspath(path)
    hwnd = int(window_id)
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        return None
    hdc_win = user32.GetDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, hdc, 2):  # 2 == PW_RENDERFULLCONTENT
            return None
        bmi = (ctypes.c_uint32 * 10)(40, w, ctypes.c_uint32(-h).value, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc, bmp, 0, h, buf, bmi, 0)  # 32bpp top-down BGRA
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(hwnd, hdc_win)
    # Crop the invisible resize border so the image matches list_windows' frame.
    fx, fy, fw, fh = _rect(hwnd)
    ox, oy = fx - wr.left, fy - wr.top
    if (ox, oy, fw, fh) != (0, 0, w, h) and fw > 0 and fh > 0:
        img = img.crop((ox, oy, ox + fw, oy + fh))
    img.save(path, "PNG")
    return {"path": path, "width": img.width, "height": img.height}


# ---------------------------------------------------------------------------
# Apps (an "app" == a process that owns a visible top-level window)
# ---------------------------------------------------------------------------

def _app_rec(pid, hwnd, fg_pid):
    name = _proc_name(pid)
    return {
        "name": name[:-4] if name.lower().endswith(".exe") else name,
        "bundle_id": _proc_path(pid),  # exe path stands in for a bundle id
        "pid": pid, "active": pid == fg_pid, "window": _title(hwnd),
    }


def list_apps():
    fg_pid = _pid_of(user32.GetForegroundWindow())
    seen = {}
    for hwnd in _enum_windows():
        pid = _pid_of(hwnd)
        if pid not in seen:
            seen[pid] = _app_rec(pid, hwnd, fg_pid)
    out = list(seen.values())
    out.sort(key=lambda a: (not a["active"], a["name"].lower()))
    return out


def frontmost_app():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = _pid_of(hwnd)
    return _app_rec(pid, hwnd, pid)


def _resolve_window(identifier):
    """Return (pid, hwnd) of the best window for a pid / exe name / exe path /
    window title, else None. UWP apps are hosted by ApplicationFrameHost, so
    match them by window title (e.g. "Calculator")."""
    ident = str(identifier)
    low = ident.lower()
    stem = low[:-4] if low.endswith(".exe") else low
    wins = []
    for hwnd in _enum_windows():
        pid = _pid_of(hwnd)
        x, y, w, h = _rect(hwnd)
        if w > 60 and h > 60:
            wins.append((hwnd, pid, _proc_name(pid).lower(), _title(hwnd), w * h))

    def best(matches):
        # Biggest window wins; ties keep z-order (front first).
        return max(matches, key=lambda m: m[4]) if matches else None

    tests = []
    if ident.isdigit():
        tests.append(lambda m: m[1] == int(ident) or m[0] == int(ident))
    tests += [
        lambda m: m[2] in (low, stem + ".exe"),
        lambda m: m[3].lower() == low,
        lambda m: _proc_path(m[1]).lower() == low,
        lambda m: stem in m[2],
        lambda m: low in m[3].lower(),
    ]
    for t in tests:
        m = best([m for m in wins if t(m)])
        if m:
            return m[1], m[0]
    return None


def app_pid(identifier):
    r = _resolve_window(identifier)
    return r[0] if r else None


def activate_app(identifier):
    r = _resolve_window(identifier)
    if not r:
        raise ValueError(f"app not found: {identifier!r}")
    pid, hwnd = r
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the process that got the last input take focus; a
    # synthetic Alt tap gives us that right.
    _send(_key(vk=0x12), _key(vk=0x12, flags=_KEYEVENTF_KEYUP))
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    name = _proc_name(pid)
    return {"name": name[:-4] if name.lower().endswith(".exe") else name, "pid": pid,
            "foreground": user32.GetForegroundWindow() == hwnd}


# ---------------------------------------------------------------------------
# UI Automation tree
# ---------------------------------------------------------------------------
_ACTIONABLE = {
    "ButtonControl", "MenuItemControl", "SplitButtonControl", "CheckBoxControl",
    "RadioButtonControl", "ComboBoxControl", "HyperlinkControl", "EditControl",
    "DocumentControl", "SliderControl", "SpinnerControl", "TabItemControl",
    "ListItemControl", "TreeItemControl", "DataItemControl", "HeaderItemControl",
}
# Action names exposed per element, and the AX names that map to them, so
# macOS habits (perform_action(app, i, "AXPress")) keep working.
_PATTERN_ACTIONS = [
    (auto.PatternId.InvokePattern, ["Invoke"]),
    (auto.PatternId.TogglePattern, ["Toggle"]),
    (auto.PatternId.SelectionItemPattern, ["Select"]),
    (auto.PatternId.ExpandCollapsePattern, ["Expand", "Collapse"]),
    (auto.PatternId.LegacyIAccessiblePattern, ["DefaultAction"]),
]
_AX_ALIASES = {"axpress": "press", "axshowmenu": "Expand", "axconfirm": "Invoke",
               "axpick": "Select", "axraise": "Focus", "axcancel": "Collapse"}
_MAX_VISITED = 4000  # UIA trees of browsers/IDEs can be huge; bound the walk
_LIVE = {}  # pid -> list of uiautomation Controls, index-aligned with the last state


def _safe(fn, default=""):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _actions(ctrl):
    acts = []
    for pid, names in _PATTERN_ACTIONS:
        pat = _safe(lambda: ctrl.GetPattern(pid), None)
        if pid == auto.PatternId.LegacyIAccessiblePattern:
            if pat is None or not _safe(lambda: pat.DefaultAction):
                continue  # only useful when it names a default action
        if pat is not None:
            acts += names
    return acts


def _value_info(ctrl):
    """(value text, settable)"""
    pat = _safe(lambda: ctrl.GetPattern(auto.PatternId.ValuePattern), None)
    if pat is None:
        return "", False
    return str(_safe(lambda: pat.Value)), not _safe(lambda: pat.IsReadOnly, True)


def get_app_state(identifier, max_elements=400, capture=True):
    """Snapshot an app's main window WITHOUT raising it or moving focus.

    Window capture via PrintWindow (works while occluded) plus a UI Automation
    walk. Each element carries screen coordinates (`center`, for the
    foreground fallback) and window-relative `rel` (1:1 with the screenshot).
    Returns {app, pid, window, window_bounds, elements:[...], text, screenshot}.
    """
    r = _resolve_window(identifier)
    if not r:
        raise ValueError(f"app not found: {identifier!r}")
    pid, hwnd = r
    x, y, w, h = _rect(hwnd)
    win = {"window_id": int(hwnd), "owner": _proc_name(pid), "pid": pid, "title": _title(hwnd),
           "x": float(x), "y": float(y), "w": float(w), "h": float(h)}

    elements, lines, live = [], [], []
    visited = [0]

    def walk(ctrl, depth):
        if len(elements) >= max_elements or visited[0] >= _MAX_VISITED:
            return
        visited[0] += 1
        role = _safe(lambda: ctrl.ControlTypeName)
        label = str(_safe(lambda: ctrl.Name))
        rect = _safe(lambda: ctrl.BoundingRectangle, None)
        offscreen = _safe(lambda: ctrl.IsOffscreen, False)
        has_box = rect is not None and rect.width() > 0 and rect.height() > 0 and not offscreen

        if has_box and (role in _ACTIONABLE or label):
            value, settable = _value_info(ctrl)
            actions = _actions(ctrl)
            idx = len(elements)
            cx, cy = rect.left + rect.width() / 2, rect.top + rect.height() / 2
            elements.append({
                "index": idx, "role": role, "label": label[:120], "value": value[:160],
                "x": float(rect.left), "y": float(rect.top),
                "w": float(rect.width()), "h": float(rect.height()),
                "center": [round(cx, 1), round(cy, 1)],
                "rel": [round(cx - x, 1), round(cy - y, 1)],
                "actions": actions, "settable": settable,
                "automation_id": str(_safe(lambda: ctrl.AutomationId)),
            })
            live.append(ctrl)
            disp = f"[{idx}] {role}"
            if label:
                disp += f" “{label[:80]}”"
            if value and value != label:
                disp += f" = {value[:60]}"
            if actions:
                disp += "  • " + "/".join(actions).lower()
            elif settable:
                disp += "  • settable"
            lines.append("  " * min(depth, 8) + disp)

        child = _safe(ctrl.GetFirstChildControl, None)
        while child is not None and len(elements) < max_elements:
            walk(child, depth + 1)
            child = _safe(child.GetNextSiblingControl, None)

    root = auto.ControlFromHandle(hwnd)
    if root is not None:
        walk(root, 0)
    _LIVE[pid] = live

    name = _proc_name(pid)
    state = {
        "app": name[:-4] if name.lower().endswith(".exe") else name,
        "pid": pid, "window": win["title"], "window_bounds": win,
        "elements": elements, "text": "\n".join(lines), "screenshot": None,
    }
    if capture:
        path = str(_screenshot_dir() / f"state-{int(time.time() * 1000)}.png")
        state["screenshot"] = capture_window(hwnd, path)
    return state


def _screenshot_dir():
    from . import ipc
    return ipc.screenshot_dir()


def _rec_for_index(state_elements, element_index):
    for e in state_elements:
        if e["index"] == element_index:
            return e
    raise ValueError(f"element_index {element_index} not in last app state")


def _live_control(pid, state_elements, element_index):
    _rec_for_index(state_elements, element_index)
    live = _LIVE.get(pid) or []
    if element_index >= len(live):
        raise ValueError("element no longer cached; call get_app_state(app) again")
    return live[element_index]


def _do(ctrl, action):
    """Run one named action. Returns the action that ran, or None."""
    a = _AX_ALIASES.get(action.lower(), action)
    if a == "press":
        # Toggle first: checkboxes often expose Invoke too, via the MSAA proxy.
        for name in ("Toggle", "Invoke", "Select", "Expand", "DefaultAction"):
            if _do_one(ctrl, name):
                return name
        return None
    return a if _do_one(ctrl, a) else None


def _do_one(ctrl, name):
    try:
        if name == "Invoke":
            p = ctrl.GetPattern(auto.PatternId.InvokePattern)
            return bool(p) and p.Invoke(waitTime=0)
        if name == "Toggle":
            p = ctrl.GetPattern(auto.PatternId.TogglePattern)
            return bool(p) and p.Toggle(waitTime=0)
        if name == "Select":
            p = ctrl.GetPattern(auto.PatternId.SelectionItemPattern)
            return bool(p) and p.Select(waitTime=0)
        if name in ("Expand", "Collapse"):
            p = ctrl.GetPattern(auto.PatternId.ExpandCollapsePattern)
            if not p:
                return False
            if name == "Expand" and p.ExpandCollapseState == auto.ExpandCollapseState.Expanded:
                return p.Collapse(waitTime=0)  # press on an open menu closes it
            return getattr(p, name)(waitTime=0)
        if name == "DefaultAction":
            p = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
            return bool(p) and p.DoDefaultAction(waitTime=0)
        if name == "Focus":
            return bool(ctrl.SetFocus())  # note: may change the foreground window
    except Exception:
        return False
    raise ValueError(f"unknown action {name!r}; use one of Invoke, Toggle, Select, "
                     "Expand, Collapse, DefaultAction, Focus (or AXPress)")


def click_element(pid, state_elements, element_index, button="left", count=1, method="ax"):
    """Activate an indexed element.

    method="ax" (default, NON-DISTURBING): run the element's UIA pattern
    (Invoke, Toggle, Select, Expand, or MSAA default action). No cursor move, no
    focus steal. Falls back to a PostMessage click on the owning window, which
    only classic Win32 controls honour.
    method="foreground": real SendInput click (moves the cursor).
    """
    rec = _rec_for_index(state_elements, element_index)
    base = {"clicked": element_index, "role": rec["role"], "label": rec["label"]}
    cx, cy = rec["center"]
    if method == "ax":
        ctrl = _live_control(pid, state_elements, element_index)
        if button == "left" and int(count) == 1:
            via = _do(ctrl, "press")
            if via:
                return {**base, "via": via}
        hwnd = _safe(lambda: ctrl.NativeWindowHandle, 0) or 0
        node = ctrl
        while not hwnd and node is not None:
            node = _safe(node.GetParentControl, None)
            hwnd = _safe(lambda: node.NativeWindowHandle, 0) if node is not None else 0
        if not hwnd:
            raise RuntimeError("no UIA action and no window handle; use method='foreground'")
        post_click_to_window(hwnd, cx, cy, button=button, count=count)
        return {**base, "via": "PostMessage", "at": [cx, cy]}
    click(cx, cy, button=button, count=count)
    return {**base, "via": "foreground", "at": [cx, cy]}


def set_element_value(pid, state_elements, element_index, value):
    """Set a text element's value via UIA ValuePattern. Non-disturbing; no keystrokes."""
    ctrl = _live_control(pid, state_elements, element_index)
    p = _safe(lambda: ctrl.GetPattern(auto.PatternId.ValuePattern), None)
    if p is None:
        raise ValueError("element has no ValuePattern (not settable)")
    if not p.SetValue(value, waitTime=0):
        raise RuntimeError("ValuePattern.SetValue failed (read-only, or the app runs elevated?)")
    return {"set_value": element_index, "chars": len(value)}


def perform_action(pid, state_elements, element_index, action):
    """Run a named UIA action (Invoke, Toggle, Select, Expand, Collapse,
    DefaultAction, Focus) or an AX alias (AXPress, AXShowMenu)."""
    ctrl = _live_control(pid, state_elements, element_index)
    via = _do(ctrl, action)
    if not via:
        raise RuntimeError(f"action {action!r} failed or is not supported by this element")
    return {"performed": via, "element_index": element_index}


_WM = {"left": (0x0201, 0x0202, 0x0001), "right": (0x0204, 0x0205, 0x0002),
       "middle": (0x0207, 0x0208, 0x0010)}  # (down, up, MK_ flag)


def post_click_to_window(hwnd, x, y, button="left", count=1):
    """Post mouse messages to the deepest child window at a screen point.
    No cursor move; only apps that read raw window messages react."""
    hwnd = int(hwnd)
    while True:
        pt = wintypes.POINT(int(x), int(y))
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        child = user32.ChildWindowFromPointEx(hwnd, pt, 0x1 | 0x4)  # skip invisible, transparent
        if not child or child == hwnd:
            break
        hwnd = child
    pt = wintypes.POINT(int(x), int(y))
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lparam = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
    down, up, mk = _WM[_norm_button(button)]
    for _ in range(int(count)):
        user32.PostMessageW(hwnd, down, mk, lparam)
        user32.PostMessageW(hwnd, up, 0, lparam)
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Permission probes. Windows has no TCC; what can block us is a locked or
# secure desktop, a non-interactive session, or UIPI against elevated apps.
# ---------------------------------------------------------------------------

def _input_desktop_ok():
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return False
    user32.CloseDesktop(h)
    return True


def has_accessibility():
    """UIA reachable and the interactive desktop is ours (not locked / UAC)."""
    return _input_desktop_ok() and _safe(lambda: auto.GetRootControl() is not None, False)


def has_screen_recording():
    try:
        ImageGrab.grab(bbox=(0, 0, 1, 1))
        return True
    except Exception:
        return False


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

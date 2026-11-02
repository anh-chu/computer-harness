"""Platform-neutral pieces shared by engine.py (macOS) and engine_win.py
(Windows), plus snapshot utilities the daemon applies to either engine's
output. Nothing here imports a platform API, so it is unit-tested everywhere.
"""
import base64
import os
import time

# ---------------------------------------------------------------------------
# Input names
# ---------------------------------------------------------------------------

def norm_button(b):
    b = (b or "left").lower()
    if b in ("r", "right"):
        return "right"
    if b in ("m", "middle"):
        return "middle"
    return "left"


# Named-key aliases both platforms accept. Each engine adds its own modifier
# aliases (e.g. "cmd" is a real key on macOS but means ctrl on Windows).
KEY_ALIASES = {
    "enter": "return", "esc": "escape", "backspace": "delete",
    "del": "forwarddelete", "pgup": "pageup", "pgdn": "pagedown",
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
    "spacebar": "space", "ctrl": "control",
    "control_l": "control", "control_r": "control", "shift_l": "shift", "shift_r": "shift",
}


def parse_chord(chord, modifiers, aliases):
    """Split 'cmd+shift+t' into (modifier names, key name), canonicalised
    through KEY_ALIASES + `aliases`. `modifiers` is the engine's set of
    canonical modifier names."""
    table = {**KEY_ALIASES, **aliases}
    mods, key = [], None
    for p in (p.strip().lower() for p in str(chord).split("+")):
        if not p:
            continue
        p = table.get(p, p)
        if p in modifiers:
            if p not in mods:
                mods.append(p)
        else:
            key = p
    if key is None:
        raise ValueError(f"no non-modifier key in chord {chord!r}")
    return mods, key


def screenshot_data_url(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


# ---------------------------------------------------------------------------
# Recording registry (each engine supplies how to start and finalize)
# ---------------------------------------------------------------------------
RECORDERS = {}  # name -> {"popen": Popen, "path": str}


def recording_status():
    return {n: {"path": r["path"], "running": r["popen"].poll() is None}
            for n, r in RECORDERS.items()}


def finish_recording(name, finalize, timeout=12):
    """Pop a recording, let `finalize(popen, timeout)` close it cleanly, and
    return {name, path, bytes}."""
    rec = RECORDERS.get(name)
    if not rec:
        raise ValueError(f"no recording named {name!r}")
    if rec["popen"].poll() is None:
        finalize(rec["popen"], timeout)
    RECORDERS.pop(name, None)
    path = rec["path"]
    return {"name": name, "path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}


def check_free(name):
    rec = RECORDERS.get(name)
    if rec and rec["popen"].poll() is None:
        raise ValueError(f"recording {name!r} already running -> {rec['path']}")


# ---------------------------------------------------------------------------
# Element kinds: one vocabulary for AX roles and UIA control types
# ---------------------------------------------------------------------------
_KINDS = {
    "button": ("AXButton", "ButtonControl", "SplitButtonControl"),
    "menubutton": ("AXMenuButton", "AXPopUpButton"),
    "link": ("AXLink", "HyperlinkControl"),
    "text": ("AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField",
             "EditControl", "DocumentControl"),
    "label": ("AXStaticText", "TextControl"),
    "checkbox": ("AXCheckBox", "CheckBoxControl"),
    "radio": ("AXRadioButton", "RadioButtonControl"),
    "combobox": ("AXComboBox", "ComboBoxControl"),
    "slider": ("AXSlider", "SliderControl"),
    "stepper": ("AXStepper", "AXIncrementor", "SpinnerControl"),
    "menu": ("AXMenu", "AXMenuBar", "MenuControl", "MenuBarControl"),
    "menuitem": ("AXMenuItem", "AXMenuBarItem", "MenuItemControl"),
    "tab": ("AXTab", "TabItemControl"),
    "tabs": ("AXTabGroup", "TabControl"),
    "list": ("AXList", "ListControl"),
    "listitem": ("AXCell", "AXRow", "ListItemControl", "DataItemControl"),
    "tree": ("AXOutline", "TreeControl"),
    "treeitem": ("AXDisclosureTriangle", "TreeItemControl"),
    "table": ("AXTable", "TableControl", "DataGridControl"),
    "header": ("AXColumn", "HeaderControl", "HeaderItemControl"),
    "toolbar": ("AXToolbar", "ToolBarControl"),
    "group": ("AXGroup", "AXSplitGroup", "AXScrollArea", "GroupControl", "PaneControl"),
    "window": ("AXWindow", "AXSheet", "AXDialog", "WindowControl"),
    "image": ("AXImage", "ImageControl"),
    "segmented": ("AXSegmentedControl",),
    "scrollbar": ("AXScrollBar", "ScrollBarControl"),
    "statusbar": ("StatusBarControl",),
    "titlebar": ("TitleBarControl",),
}
_KIND_OF = {role: kind for kind, roles in _KINDS.items() for role in roles}


def kind_of(role):
    """'AXButton' / 'ButtonControl' -> 'button'; unknown roles -> 'other'."""
    return _KIND_OF.get(role, "other")


# ---------------------------------------------------------------------------
# Snapshot utilities (operate on the dict get_app_state returns)
# ---------------------------------------------------------------------------

def element_line(e):
    """One line of the numbered tree text, without depth indent."""
    s = f"[{e['index']}] {e['kind']}"
    if e.get("label"):
        s += f" “{e['label'][:80]}”"
    if e.get("value") and e["value"] != e.get("label"):
        s += f" = {e['value'][:60]}"
    if e.get("actions"):
        s += "  • " + "/".join(a.removeprefix("AX").lower() for a in e["actions"])
    elif e.get("settable"):
        s += "  • settable"
    return s


def render_text(elements):
    """The numbered, indented tree an agent reads to pick an element."""
    return "\n".join("  " * min(e.get("depth", 0), 8) + element_line(e) for e in elements)


def is_interactive(e):
    return bool(e.get("actions") or e.get("settable"))


def find_elements(elements, label=None, role=None):
    """Elements matching `label` (case-insensitive; exact matches first, then
    substrings) and `role` (a kind like 'button' or a native role)."""
    def role_ok(e):
        return role is None or role in (e.get("kind"), e.get("role"))

    cands = [e for e in elements if role_ok(e)]
    if label is None:
        return cands
    low = label.lower()
    exact = [e for e in cands if e.get("label", "").lower() == low]
    if exact:
        return exact
    return [e for e in cands if low in e.get("label", "").lower()
            or low in e.get("value", "").lower()]


def resolve_index(elements, element_index=None, label=None, role=None):
    """Turn (element_index | label/role) into one index, or raise with the
    candidates so the caller can pick."""
    if element_index is not None:
        return int(element_index)
    if label is None and role is None:
        raise ValueError("pass element_index, or label= / role=")
    hits = find_elements(elements, label, role)
    if not hits:
        raise ValueError(f"no element matches label={label!r} role={role!r}; call get_app_state again?")
    if len(hits) > 1:
        # Prefer one the agent can act on; still ambiguous -> make it choose.
        acting = [e for e in hits if is_interactive(e)]
        if len(acting) == 1:
            return acting[0]["index"]
        listing = "; ".join(element_line(e) for e in hits[:8])
        raise ValueError(f"{len(hits)} elements match label={label!r} role={role!r}: {listing}. "
                         "Pass role= or element_index=")
    return hits[0]["index"]


def shape_state(state, previous=None, interactive_only=False, diff=False):
    """Return the copy of a full snapshot that is sent to the caller.

    interactive_only drops elements without actions or a settable value (their
    indices stay as they are, so they still match the daemon's cache).
    diff adds {"added": [...], "removed": [...]} tree lines vs `previous`.
    """
    out = dict(state)
    if interactive_only:
        keep = [e for e in state["elements"] if is_interactive(e)]
        out["elements"] = keep
        out["text"] = render_text(keep)
    if diff:
        def lines(st):
            return {element_line({**e, "index": "·"}) for e in (st or {}).get("elements", [])}
        now, before = lines(state), lines(previous)
        out["diff"] = {"added": sorted(now - before), "removed": sorted(before - now),
                       "first_snapshot": previous is None}
    return out


def annotate(src, elements, dst):
    """Draw each element's box and index on a copy of the window screenshot.
    Element `rel` centres are window-relative, 1:1 with the image."""
    from PIL import Image, ImageDraw
    img = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(img)
    for e in elements:
        cx, cy = e["rel"]
        w, h = e["w"], e["h"]
        x0, y0 = cx - w / 2, cy - h / 2
        if x0 > img.width or y0 > img.height or x0 + w < 0 or y0 + h < 0:
            continue
        color = (230, 40, 90) if is_interactive(e) else (40, 120, 230)
        draw.rectangle([x0, y0, x0 + w, y0 + h], outline=color, width=2)
        tag = str(e["index"])
        tw = 7 * len(tag) + 4
        tx = max(0, x0)
        ty = y0 - 13 if y0 >= 13 else max(0, y0)  # above the box when there is room
        draw.rectangle([tx, ty, tx + tw, ty + 13], fill=color)
        draw.text((tx + 2, ty), tag, fill=(255, 255, 255))
    img.save(dst, "PNG")
    return dst


# ---------------------------------------------------------------------------
# Capture retention
# ---------------------------------------------------------------------------

def prune(directory, keep_files, keep_days, now=None):
    """Delete the oldest files beyond `keep_files`, and any older than
    `keep_days`. Returns the number deleted. 0 disables a limit."""
    now = now or time.time()
    try:
        files = sorted((e for e in os.scandir(directory) if e.is_file()),
                       key=lambda e: e.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        return 0
    doomed = []
    for i, e in enumerate(files):
        too_many = keep_files and i >= keep_files
        too_old = keep_days and now - e.stat().st_mtime > keep_days * 86400
        if too_many or too_old:
            doomed.append(e.path)
    for p in doomed:
        try:
            os.remove(p)
        except OSError:
            pass
    return len(doomed)

"""End-to-end smoke test for the Windows engine, through the real CLI daemon.

Opens a small WinForms window, puts Notepad in front of it, then drives the
form in the background and checks that the cursor and the foreground window
did not change. Run on a signed-in Windows desktop:

    python tests/smoke_windows.py
"""
import os
import shutil
import subprocess
import sys
import time

from PIL import Image

from computer_harness.admin import daemon_alive, ensure_daemon, restart_daemon
from computer_harness.helpers import *  # noqa: F401,F403

TITLE = "CH Smoke"
FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form; $f.Text = 'CH Smoke'; $f.Width = 420; $f.Height = 260
$b = New-Object Windows.Forms.Button; $b.Text = 'Press me'; $b.Left = 20; $b.Top = 20; $b.Width = 140
$t = New-Object Windows.Forms.TextBox; $t.Left = 20; $t.Top = 70; $t.Width = 340
$c = New-Object Windows.Forms.CheckBox; $c.Text = 'Check me'; $c.Left = 20; $c.Top = 110
$b.Add_Click({ $b.Text = 'Pressed' })
$f.Controls.AddRange(@($b, $t, $c))
$f.StartPosition = 'Manual'; $f.Left = 100; $f.Top = 100
[Windows.Forms.Application]::Run($f)
"""

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


def find(st, role, label=None):
    for e in st["elements"]:
        if e["role"] == role and (label is None or e["label"] == label):
            return e
    raise AssertionError(f"no {role} {label!r} in tree:\n{st['text']}")


def wait_for_window(title, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(w["title"] == title for w in list_windows()):
            return
        time.sleep(0.5)
    raise AssertionError(f"window {title!r} never appeared")


def not_blank(path):
    lo_hi = Image.open(path).convert("L").getextrema()
    return lo_hi[1] - lo_hi[0] > 10


def cursor():
    import ctypes
    from ctypes import wintypes
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def main():
    restart_daemon()
    ensure_daemon()
    perms = permissions()
    check("permissions", perms["accessibility"] and perms["screen_recording"], perms)
    info = screen_info()
    check("screen_info", info["width"] > 0 and info["height"] > 0, info)

    form = subprocess.Popen(["powershell", "-NoProfile", "-Command", FORM])
    note = subprocess.Popen(["notepad.exe"])
    try:
        wait_for_window(TITLE)
        time.sleep(1.5)  # let Notepad open on top
        activate_app("notepad")
        fg_before = frontmost_app()
        cur_before = cursor()
        check("notepad in front", fg_before["name"].lower() == "notepad", fg_before)

        # --- background (non-disturbing) tier ---
        st = get_app_state(TITLE)
        check("get_app_state tree", len(st["elements"]) >= 3, f"{len(st['elements'])} elements")
        shot = st["screenshot"]
        check("background window capture", shot and os.path.getsize(shot["path"]) > 0
              and not_blank(shot["path"]), shot)

        def focus_kept(step):
            fg = frontmost_app()
            check(f"focus kept after {step}", fg["pid"] == fg_before["pid"], fg)

        btn = find(st, "ButtonControl", "Press me")
        r = click(TITLE, btn["index"])
        check("click via UIA", r["via"] in ("Invoke", "DefaultAction"), r)
        focus_kept("click")

        edit = find(st, "EditControl")
        set_value(TITLE, edit["index"], "hello ✓")
        focus_kept("set_value")
        box = find(st, "CheckBoxControl", "Check me")
        r = click(TITLE, box["index"])
        check("checkbox via UIA", r["via"] == "Toggle", r)
        focus_kept("toggle")

        time.sleep(0.5)
        st2 = get_app_state(TITLE, screenshot=False)
        labels = [e["label"] for e in st2["elements"]]
        check("button reacted", "Pressed" in labels, labels)
        check("set_value", find(st2, "EditControl")["value"] == "hello ✓", find(st2, "EditControl"))

        check("cursor untouched", cursor() == cur_before, (cur_before, cursor()))

        # --- foreground tier ---
        a = activate_app(TITLE)
        check("activate_app", a["foreground"], a)
        st3 = get_app_state(TITLE, screenshot=False)
        e3 = find(st3, "EditControl")
        click(TITLE, e3["index"], method="foreground")
        press_key("end")
        press_key("shift+home")  # old WinForms text boxes ignore Ctrl+A
        type_text("typed")
        time.sleep(0.5)
        st4 = get_app_state(TITLE, screenshot=False)
        check("foreground typing", find(st4, "EditControl")["value"] == "typed",
              find(st4, "EditControl"))

        full = capture_screenshot()
        check("full screenshot", full["width"] == info["width"] and not_blank(full["path"]), full)

        if shutil.which("ffmpeg"):
            rec = record_screen(seconds=2)
            check("record_screen", rec["bytes"] > 0, rec)
        else:
            print("SKIP  record_screen  (no ffmpeg)")
    finally:
        form.kill()
        note.kill()
        subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"], capture_output=True)

    restart_daemon()
    check("daemon stops", not daemon_alive())
    print(f"\n{len(failures)} failed: {failures}" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

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


def pick(st, role, label=None):
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

        def focus_kept(step):
            fg = frontmost_app()
            check(f"focus kept after {step}", fg["pid"] == fg_before["pid"], fg)

        # --- background (non-disturbing) tier ---
        time.sleep(1)
        focus_kept("settle")
        st = get_app_state(TITLE)
        focus_kept("get_app_state")
        check("get_app_state tree", len(st["elements"]) >= 3, f"{len(st['elements'])} elements")
        shot = st["screenshot"]
        check("background window capture", shot and os.path.getsize(shot["path"]) > 0
              and not_blank(shot["path"]), shot)

        marked = shot.get("annotated_path")
        check("annotated screenshot", marked and os.path.exists(marked)
              and Image.open(marked).tobytes() != Image.open(shot["path"]).tobytes(), marked)
        btn = pick(st, "ButtonControl", "Press me")
        check("kind field", btn["kind"] == "button", btn)
        small = get_app_state(TITLE, screenshot=False, interactive_only=True)
        check("interactive_only", 0 < len(small["elements"]) < len(st["elements"])
              and all(e["actions"] or e["settable"] for e in small["elements"]),
              f"{len(small['elements'])} of {len(st['elements'])}")

        r = click(TITLE, label="press me")
        check("click by label via UIA", r["via"] in ("Invoke", "DefaultAction"), r)
        focus_kept("click")

        edit = pick(st, "EditControl")
        set_value(TITLE, edit["index"], "hello ✓")
        focus_kept("set_value")
        r = click(TITLE, label="Check me", role="checkbox")
        check("checkbox via UIA", r["via"] == "Toggle", r)
        focus_kept("toggle")

        # A second daemon must refuse to start and leave the cache alone.
        dup = subprocess.run([sys.executable, "-m", "computer_harness.daemon", "default"],
                             capture_output=True, text=True, timeout=30)
        check("second daemon refused", dup.returncode == 0 and "already running" in dup.stderr,
              dup.stderr.strip()[-200:])
        check("cache survives", len(find(TITLE, label="Check me")) == 1)

        st2 = get_app_state(TITLE, screenshot=False, diff=True)  # vs the snapshot before the click
        check("diff", any("Pressed" in line for line in st2["diff"]["added"]), st2["diff"])
        hit = wait_for(TITLE, label="Pressed", timeout=5)
        check("wait_for", hit["kind"] == "button", hit)
        labels = [e["label"] for e in st2["elements"]]
        check("button reacted", "Pressed" in labels, labels)
        check("set_value", pick(st2, "EditControl")["value"] == "hello ✓", pick(st2, "EditControl"))

        check("cursor untouched", cursor() == cur_before, (cur_before, cursor()))

        # --- foreground tier ---
        a = activate_app(TITLE)
        check("activate_app", a["foreground"], a)
        st3 = get_app_state(TITLE, screenshot=False)
        t0 = time.time()
        r = click(TITLE, label="Pressed")
        check("no focus guard when app in front", not r["focus_restored"] and time.time() - t0 < 0.15,
              (r, round(time.time() - t0, 3)))
        e3 = pick(st3, "EditControl")
        click(TITLE, e3["index"], method="foreground")
        press_key("end")
        press_key("shift+home")  # old WinForms text boxes ignore Ctrl+A
        type_text("typed")
        time.sleep(0.5)
        st4 = get_app_state(TITLE, screenshot=False)
        check("foreground typing", pick(st4, "EditControl")["value"] == "typed",
              pick(st4, "EditControl"))

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

"""End-to-end smoke test for the macOS engine, through the real CLI daemon.

Opens a TextEdit document behind Finder and drives it in the background.
Exits 0 with SKIP lines when this process lacks the Accessibility or Screen
Recording grant (as on most CI machines):

    python tests/smoke_macos.py
"""
import os
import subprocess
import sys
import time

from computer_harness.admin import daemon_alive, ensure_daemon, restart_daemon
from computer_harness.helpers import *  # noqa: F401,F403

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


def osa(script):
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=30)


def main():
    restart_daemon()
    ensure_daemon()
    perms = permissions()
    print("permissions:", perms)
    check("screen_info", screen_info()["width"] > 0)
    if not perms["accessibility"]:
        print("SKIP  accessibility tests (no Accessibility grant for this process)")
        restart_daemon()
        sys.exit(1 if failures else 0)

    osa('tell application "TextEdit" to make new document')
    osa('tell application "Finder" to activate')
    time.sleep(2)
    fg = frontmost_app()
    try:
        st = get_app_state("TextEdit")
        check("get_app_state tree", len(st["elements"]) > 0, f"{len(st['elements'])} elements")
        check("kind field", all("kind" in e for e in st["elements"]))
        texts = find("TextEdit", role="text")
        check("find text area", len(texts) >= 1, texts[:1])
        if perms["screen_recording"] and st["screenshot"]:
            marked = st["screenshot"].get("annotated_path")
            check("annotated screenshot", marked and os.path.exists(marked), marked)
        else:
            print("SKIP  screenshots (no Screen Recording grant)")

        r = set_value("TextEdit", texts[0]["index"], value="hello ✓")
        check("set_value", r["chars"] == 7 and r["focus_restored"] is False, r)
        st2 = get_app_state("TextEdit", screenshot=False, diff=True)
        check("value read back", any(e["value"] == "hello ✓" for e in st2["elements"]))
        check("diff", st2["diff"]["first_snapshot"] is False, st2["diff"])
        check("focus untouched", frontmost_app()["pid"] == fg["pid"], frontmost_app())

        dup = subprocess.run([sys.executable, "-m", "computer_harness.daemon", "default"],
                             capture_output=True, text=True, timeout=60)
        check("second daemon refused", "already running" in dup.stderr, dup.stderr.strip()[-200:])
        check("cache survives", len(find("TextEdit", role="text")) >= 1)
    finally:
        subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit saving no'],
                       capture_output=True)

    restart_daemon()
    check("daemon stops", not daemon_alive())
    print(f"\n{len(failures)} failed: {failures}" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

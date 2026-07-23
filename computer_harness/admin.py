"""Daemon lifecycle: ensure / ping / restart / status / doctor."""
import os
import subprocess
import sys
import time

from . import ipc


def daemon_alive(name=None):
    return ipc.ping(name or ipc.NAME)


def ensure_daemon(name=None, wait=20.0):
    name = name or ipc.NAME
    if daemon_alive(name):
        return
    log = ipc.log_path(name)
    env = {**os.environ, "CH_NAME": name}
    with open(log, "ab") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "computer_harness.daemon", name],
            stdout=lf, stderr=lf, start_new_session=True, env=env,
        )
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_alive(name):
            return
        time.sleep(0.15)
    tail = ""
    try:
        tail = ipc.log_path(name).read_text()[-2000:]
    except OSError:
        pass
    raise RuntimeError(f"daemon {name} didn't come up. Log:\n{tail}")


def restart_daemon(name=None):
    name = name or ipc.NAME
    if daemon_alive(name):
        try:
            ipc.call({"meta": "shutdown"}, name=name, timeout=3)
        except Exception:
            pass
    # hard cleanup if a stale pid lingers — but only SIGTERM a process we can
    # confirm is actually our daemon, so a recycled PID isn't signalled.
    try:
        pid = int(ipc.pid_path(name).read_text().strip())
        cmd = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True,
        ).stdout
        if "computer_harness.daemon" in cmd:
            os.kill(pid, 15)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass
    ipc.cleanup(name)
    time.sleep(0.3)


def status(name=None):
    name = name or ipc.NAME
    if not daemon_alive(name):
        return {"alive": False, "name": name}
    r = ipc.call({"meta": "ping"}, name=name)
    perms = ipc.call({"op": "permissions"}, name=name)
    return {"alive": True, "name": name, **r, "permissions": perms}


def run_doctor(name=None):
    name = name or ipc.NAME
    print(f"computer-harness doctor (name={name})")
    print(f"  python:  {sys.executable}")
    try:
        import Quartz  # noqa
        import AppKit  # noqa
        import ApplicationServices  # noqa
        print("  pyobjc:  OK (Quartz, AppKit, ApplicationServices)")
    except Exception as e:
        print(f"  pyobjc:  MISSING — {e}")
        print("           install: pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices")
        return 1
    alive = daemon_alive(name)
    print(f"  daemon:  {'alive' if alive else 'not running (will auto-start on first use)'}")
    if not alive:
        try:
            ensure_daemon(name)
            alive = True
            print("           started OK")
        except Exception as e:
            print(f"           FAILED to start: {e}")
            return 1
    perms = ipc.call({"op": "permissions"}, name=name)
    ax = perms.get("accessibility")
    sr = perms.get("screen_recording")
    print(f"  accessibility grant:    {'GRANTED' if ax else 'MISSING'}")
    print(f"  screen recording grant: {'GRANTED' if sr else 'MISSING'}")
    if not (ax and sr):
        print()
        print("  Grant permissions to the python binary above:")
        print("    System Settings > Privacy & Security > Accessibility")
        print("    System Settings > Privacy & Security > Screen Recording")
        print("  Then: computer-harness --restart")
        return 2
    info = ipc.call({"op": "screen_info"}, name=name)
    print(f"  display: {info['width']}x{info['height']} pts @ {info['scale']}x")
    print("  all good.")
    return 0

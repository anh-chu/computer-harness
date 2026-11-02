import sys

from .admin import daemon_alive, ensure_daemon, restart_daemon, run_doctor, status
from .helpers import *  # noqa: F401,F403 — pre-imported into the heredoc namespace
from . import ipc

HELP = """Computer Harness — native macOS and Windows computer use

Read SKILL.md for the workflow. Helpers are pre-imported; the daemon auto-starts.

Typical usage:
  computer-harness <<'PY'
  print(screen_info())
  shot = capture_screenshot()
  print(shot["path"])            # then read that path to see the screen
  click_at_xy(200, 300)
  type_text("hello")
  press_key("Return")
  PY

Indexed (accessibility) flow:
  computer-harness <<'PY'
  st = get_app_state("Notes")
  print(st["text"])              # numbered, actionable elements
  click("Notes", element_index=4)
  PY

Commands:
  computer-harness --doctor      diagnose install, daemon, and TCC permissions
  computer-harness --status      daemon + permission status as JSON
  computer-harness --restart     stop the daemon (next call starts fresh)
  computer-harness mcp [--http --host <ip> --self-signed] [--allow-foreground]
                                 serve the helpers as MCP tools (needs the [mcp] extra)
  computer-harness --help
"""


def main():
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] in {"--doctor", "doctor"}:
        sys.exit(run_doctor())
    if args and args[0] in {"--status", "status"}:
        import json
        print(json.dumps(status(), indent=2))
        return
    if args and args[0] in {"mcp", "--mcp"}:
        from .mcp_server import main as mcp_main
        mcp_main(args[1:])
        return
    if args and args[0] in {"--restart", "--reload"}:
        restart_daemon()
        print("daemon stopped — will restart fresh on next call")
        return

    if not args and not sys.stdin.isatty():
        code = sys.stdin.read()
        if not code.strip():
            sys.exit(HELP)
    else:
        sys.exit(HELP)

    ensure_daemon()
    exec(code, globals())


if __name__ == "__main__":
    main()

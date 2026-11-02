# computer-harness

The simplest, thinnest harness to control **native macOS and Windows apps** with your agent —
and, by default, **without disturbing you**. A desktop sibling to browser-harness:
same daemon + heredoc shape, but it drives any native app, not just Chrome.

```bash
computer-harness <<'PY'
st = get_app_state("Notes")       # background screenshot + numbered a11y tree
click("Notes", element_index=4)   # AXPress — runs in the background
set_value("Notes", 7, "hello")    # set a text field directly, no keystrokes
PY
```

## Why

macOS's ChatGPT/Codex desktop app ships computer use whose best property is that
**it doesn't disturb you** — the agent works in one app while you keep working in
another. Under the hood that's OpenAI's `@oai/sky`: a signed helper that captures
a specific window and acts through the accessibility API over a local socket.

computer-harness rebuilds that architecture on **public Apple APIs** — no private
entitlements, no dependency on OpenAI's binary:

- **Input** — CoreGraphics `CGEvent` (global foreground) and `CGEventPostToPid`
  + `AXUIElementPerformAction` / set-value (background, non-disturbing).
- **Pixels** — `CGWindowListCreateImage` captures a window even when it's
  occluded or not frontmost; `screencapture` for full-screen and video.
- **Structure** — the `AXUIElement` accessibility tree, surfaced as an indexed,
  clickable element list (sky's interaction model).

## Two tiers

- **Non-disturbing (default):** operate on a target app's window in the
  background — no cursor move, no focus steal, so you keep working in the
  foreground. `get_app_state`, `click`, `set_value`, `perform_action`,
  `capture_window`, `list_windows`, plus passive `capture_screenshot` and screen
  recording. Works reliably on standard AppKit apps.
- **Foreground (explicit, interrupts you):** global cursor/keyboard for canvas
  apps and games. `click_at_xy`, `type_text`, `press_key`, `drag`, `scroll`,
  `activate_app`. Opt-in only.

## Install

macOS (CoreGraphics, CGWindowList, Accessibility API) or Windows 10/11 (see
below).

```bash
uv tool install git+https://github.com/anh-chu/computer-harness
computer-harness --doctor
```

Or from a clone: `python3 -m venv .venv && .venv/bin/pip install -e .`

Grant the printed Python binary **Accessibility** and **Screen Recording** in
System Settings → Privacy & Security (once). `--doctor` tells you what's missing.
See [install.md](install.md) for details, [SKILL.md](SKILL.md) for the full
workflow.

## Windows

The same helpers run on Windows 10/11 through `engine_win.py`:

- **Structure and background actions:** UI Automation (Invoke, Toggle, Select,
  ExpandCollapse, ValuePattern.SetValue), so `click` and `set_value` stay
  non-disturbing.
- **Pixels:** `PrintWindow(PW_RENDERFULLCONTENT)` for a covered window, Pillow
  `ImageGrab` for the screen, ffmpeg `gdigrab` for video.
- **Foreground input:** `SendInput`.
- **Daemon channel:** 127.0.0.1 on a random port plus a per-run token, because
  asyncio has no AF_UNIX server on Windows.

Some Win32 apps raise their own window when a button is pressed through UIA;
background actions detect that and give focus back to your window within
~150 ms. Limits: no capture of minimized windows; the background click fallback reaches
only classic Win32 controls; apps running as admin need an elevated daemon; a
locked screen or UAC prompt blocks everything. See [install.md](install.md#windows).

## MCP server

`computer-harness mcp` serves the helpers as MCP tools, so an agent on another
machine can drive this one. Install with the extra: `uv tool install
"computer-harness[mcp] @ git+https://github.com/anh-chu/computer-harness"`.

```bash
computer-harness mcp                                          # stdio, local client
computer-harness mcp --http --host 192.168.1.20 --self-signed # HTTPS on the LAN
computer-harness mcp --http --host 192.168.1.20 --self-signed --allow-foreground
```

- Clients get the listed tools only, never arbitrary Python.
- Every HTTP request needs `Authorization: Bearer <token>` (`CH_MCP_TOKEN`, or
  `~/.computer-harness/mcp-token`, created owner-only on first run).
- A LAN address needs TLS: `--self-signed` makes a certificate for this host's
  names and IPs and prints its SHA-256 fingerprint; or pass `--cert`/`--key`.
- The `Host` header must be one of this machine's names or IPs, or an
  `--allowed-host`.
- Foreground tools (real cursor and keyboard) appear only with
  `--allow-foreground`.
- Snapshots and captures come back as images, not file paths.

See [install.md](install.md#mcp-server) for client setup.

## Security model — read before use

computer-harness is a "run code as me" tool, like a shell. Specifically:

- **It executes arbitrary Python from stdin as your user** (`run.py`) and can
  synthesize input and read your screen. Only pipe it code you trust.
- **The daemon is a standing proxy to your Accessibility + Screen Recording
  grants.** Its control socket is `0700` (owner-only), so **another user cannot
  reach it** — but *any process running as you* can connect and drive it, whether
  or not that process was itself granted those TCC permissions. That is by design
  for a single-user machine; on a shared account, treat the daemon as ambient
  authority and stop it (`--restart` kills it) when unused.
- **Screen captures may contain secrets.** Screenshots and recordings land
  unencrypted in `~/.computer-harness/` (dirs `0700`, files `0600` — owner-only).
  The daemon keeps the newest 500 files of each kind and deletes anything older
  than 7 days (`CH_KEEP_FILES`, `CH_KEEP_DAYS`; `0` turns a limit off). Delete
  the directory to purge.
- **No network egress.** The tool makes no outbound connections and sends no
  telemetry; the only socket is the local `AF_UNIX` control channel.

## Status

v0.2.0. macOS and Windows. CI runs unit tests on Linux, macOS and Windows,
and end-to-end tests on macOS (TextEdit) and Windows Server 2022/2025 (WinForms). Verified end-to-end: background window capture, AX-action
clicks, direct text set-value, full-screen foreground input, and desktop video
recording — all through the CLI/daemon. Not affiliated with OpenAI.

## License

MIT — see [LICENSE](LICENSE).

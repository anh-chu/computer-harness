# Installing computer-harness

macOS (CoreGraphics, CGWindowList, Accessibility API) or Windows 10/11 (UI
Automation, SendInput, PrintWindow). Windows steps are in the last section.

## 1. Runtime

Quickest: `uv tool install git+https://github.com/anh-chu/computer-harness`
puts `computer-harness` on `$PATH` (macOS or Windows). Grant the permissions in
step 2 to the Python that `--doctor` prints.

From a clone instead:

Already installed in this environment at `~/tools/computer-harness/` with a
dedicated venv (`~/tools/computer-harness/.venv`) and a `computer-harness`
symlink on `$PATH` (`~/.local/bin/computer-harness`).

To recreate on a new machine:

```bash
cd ~/tools/computer-harness
python3 -m venv .venv
.venv/bin/pip install -e .          # installs pyobjc + pillow, registers the CLI
ln -sf ~/tools/computer-harness/.venv/bin/computer-harness ~/.local/bin/computer-harness
```

Verify: `computer-harness --doctor`.

## 2. Permissions (grant once)

The daemon process (the venv's `python3`) must hold two TCC grants. macOS will
prompt on first use, or grant manually:

- **System Settings → Privacy & Security → Accessibility** — lets it read the
  a11y tree and perform AXPress / set-value. Required for the non-disturbing tier.
- **System Settings → Privacy & Security → Screen Recording** — lets
  `screencapture` / `CGWindowListCreateImage` read window pixels.

Add the binary that `--doctor` prints under `python:` (the venv python). If you
run computer-harness from a terminal or from Claude Code, the *responsible* app
(Terminal / iTerm / the Claude process) may be what the grant attaches to — if
`--doctor` still shows a grant missing after you added Python, add that host app
too. After granting: `computer-harness --restart`, then `--doctor` again.

`--doctor` prints GRANTED / MISSING for each so you know exactly what's needed.

## 3. Smoke test

```bash
computer-harness <<'PY'
print(screen_info())
print([a["name"] for a in list_apps()][:10])
st = get_app_state("Finder")
print(st["screenshot"]["path"])     # read this path to see the captured window
PY
```

## Layout

```
~/tools/computer-harness/
  computer_harness/
    engine.py     macOS (pyobjc): CGEvent input, screencapture/CGWindowList pixels, AXUIElement tree
    engine_win.py Windows: SendInput, PrintWindow/ImageGrab pixels, UI Automation tree
    common.py     shared: chords, element kinds, label lookup, annotation, retention
    daemon.py     AF_UNIX JSON server; holds TCC grants, caches last app state
    ipc.py        socket framing + paths
    admin.py      daemon lifecycle + doctor
    helpers.py    model-facing verbs (pre-imported into heredocs)
    run.py        CLI entry
  agent-workspace/
    agent_helpers.py   evidence_shot / FlowRecorder / pixel_diff
  SKILL.md
  install.md
```

## Runtime files

- Socket + pid: `/tmp/ch-<name>.sock`, `/tmp/ch-<name>.pid` (socket is `0700`)
- Log: `~/.computer-harness/ch-<name>.log`
- Screenshots: `~/.computer-harness/screenshots/`
- Recordings: `~/.computer-harness/recordings/`

⚠ **Privacy:** screenshots and recordings capture whatever is on screen and may
contain passwords, tokens, or private messages. They are stored **unencrypted**
in `~/.computer-harness/` (dirs `0700`, files `0600` — owner-only). The daemon
keeps the newest 500 files per folder and deletes files older than 7 days; set
`CH_KEEP_FILES` / `CH_KEEP_DAYS` before it starts to change that (`0` = no
limit). Delete the directory to purge, or uninstall (below).

## Multiple instances

Set `CH_NAME=work` to run an isolated daemon (own socket/log). Default name is
`default`. Only one daemon runs per name: a second one exits at once, and a
busy daemon is waited for rather than replaced.

## Uninstall

```bash
computer-harness --restart          # stop the daemon
rm ~/.local/bin/computer-harness
rm -rf ~/tools/computer-harness/.venv ~/.computer-harness
# then remove the grants in System Settings if desired
```

## Windows

```powershell
git clone https://github.com/anh-chu/computer-harness; cd computer-harness
py -m venv .venv; .venv\Scripts\pip install -e .   # uiautomation, psutil, pillow
winget install Gyan.FFmpeg                           # optional, for recording
.venv\Scripts\computer-harness --doctor
```

No permission grants. `--doctor` checks that the desktop is unlocked and
interactive, whether the daemon is elevated (needed to drive apps that run as
admin), and whether ffmpeg is present.

Runtime files all live in `%USERPROFILE%\.computer-harness\`. The daemon
listens on a random 127.0.0.1 port; `ch-<name>.port` holds the port and a
random token that every client must send first.

`tests/smoke_windows.py` drives a test window end to end and checks that the
cursor and focus do not move. CI runs it on `windows-2022` and `windows-2025`,
`tests/smoke_macos.py` on `macos-latest`, and `tests/test_common.py` everywhere.

## MCP server

Install the extra (Python 3.10+): `uv tool install "computer-harness[mcp] @ git+https://github.com/anh-chu/computer-harness"`,
or `pip install -e ".[mcp]"` in a clone.

On the machine to control, from its signed-in desktop session:

```bash
computer-harness mcp --http --host <its LAN IP> --self-signed [--allow-foreground]
```

It prints the URL, the token file, the certificate path and its SHA-256
fingerprint. The server refuses a LAN address without TLS.

On the client machine:

1. Copy `~/.computer-harness/mcp-tls/cert.pem` from the server and check its
   fingerprint: `openssl x509 -in cert.pem -noout -fingerprint -sha256`.
2. Trust it for the client. Node-based clients (Claude Code):
   `export NODE_EXTRA_CA_CERTS=/path/to/cert.pem`. Python (httpx):
   `verify=ssl.create_default_context(cafile="cert.pem")`.
3. Add the server with the token from the server's `~/.computer-harness/mcp-token`:

```bash
claude mcp add --transport http computer-harness https://<host>:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Use the address the certificate covers: the bound IP, the hostname, or
`<hostname>.local`. Add more names with `--allowed-host` (they also go into a
regenerated certificate).

Keep the server in the user's login session, not a system service: macOS
permissions and Windows UI Automation both need the desktop session. Start
it at login with a LaunchAgent (macOS) or a Task Scheduler "At log on" task
(Windows). Rotate the token by deleting `mcp-token` and restarting.

CI runs `tests/smoke_mcp.py` on macOS and Windows: it binds the runner's LAN IP
over HTTPS and drives TextEdit / a WinForms window through the MCP client.

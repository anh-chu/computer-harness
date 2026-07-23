# Installing computer-harness

macOS only (uses CoreGraphics, CGWindowList, and the Accessibility API).

## 1. Runtime

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
    engine.py     pyobjc: CGEvent input, screencapture/CGWindowList pixels, AXUIElement tree
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
in `~/.computer-harness/` (dirs `0700`, files `0600` — owner-only) and are **not
pruned automatically**. Delete the directory to purge, or uninstall (below).

## Multiple instances

Set `CH_NAME=work` to run an isolated daemon (own socket/log). Default name is
`default`.

## Uninstall

```bash
computer-harness --restart          # stop the daemon
rm ~/.local/bin/computer-harness
rm -rf ~/tools/computer-harness/.venv ~/.computer-harness
# then remove the grants in System Settings if desired
```

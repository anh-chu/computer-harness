# computer-harness

Direct native-app control on macOS (CoreGraphics input, CGWindowList pixels,
Accessibility API structure) and Windows (SendInput, PrintWindow, UI Automation). The desktop sibling of `browser-harness`:
same daemon + heredoc shape, but it drives **any native app**, not just Chrome.
Reverse-engineered from the architecture of Codex's computer use (`@oai/sky`) —
same interaction model (per-window screenshot + indexed accessibility tree +
click-by-coordinate-or-element), rebuilt on public Apple APIs.

**The headline property: it does not disturb you.** The default verbs operate on
a target app's window *in the background* — no cursor movement, no focus steal —
so you keep working in the foreground while the agent works in another app.

## Usage

```bash
computer-harness <<'PY'
st = get_app_state("Notes")     # background screenshot + numbered a11y tree
print(st["text"])
click("Notes", element_index=4) # AXPress in the background — you aren't touched
PY
```

- Invoke as `computer-harness` — it's on `$PATH`. No cd, no venv activation.
- Use the heredoc form for every multi-line command (prevents shell quote mangling).
- Helpers are pre-imported; the daemon auto-starts and holds the TCC grants.
- macOS and Windows. On macOS, first run needs two permissions granted once (see
  install.md). On Windows, read the Windows section at the end.

## Non-disturbance is a HARD DEFAULT — ALWAYS APPLY

The user is usually still working on this Mac while the agent runs.
computer-harness must **never steal focus or move the cursor while the user is
active.** This is the tool's whole reason to exist.

- **Default to the non-disturbing tier** (`get_app_state`, `click`, `set_value`,
  `perform_action`, `capture_window`, `list_*`, plus passive `capture_screenshot`
  / recording). These touch a target app's window in the background and never
  grab focus.
- **The foreground tier is opt-in, not a fallback you reach for silently.**
  `activate_app`, `click_at_xy`, `type_text`, `press_key`, `drag`, `scroll`, and
  AppleScript `activate` all steal system-wide focus on the main display. Do NOT
  use them while the user is working unless they have explicitly said "take over
  the screen / I'm stepping away."
- **If a task genuinely needs foreground control** (a screen demo of visible
  apps, a Catalyst/canvas app with no AX): first confirm the user is away, OR run
  it on a **separate macOS Space / second display** so their active workspace
  keeps focus. Never assume it's fine to drive the main display.
- Field lesson: a recorded demo used the foreground tier and repeatedly yanked
  focus from a browser window on another display while the user was working in
  it. Passive screenshots/recording were fine; the `activate`/click/type calls
  were the culprit.

## Safety — ALWAYS APPLY

Computer use is high-risk: a click or keystroke can send a message, delete a
file, confirm a purchase, transition a ticket, or overwrite work — often
irreversibly and in the user's real, logged-in apps.

- **Destructive or outward-facing actions require the user's explicit approval first.**
  That includes: deleting/moving/overwriting anything, sending or posting
  (Slack/email/messages/social/PRs), confirming dialogs (Delete, Send, Buy, OK
  on a warning), submitting forms, purchases/payments, changing settings, or any
  action you cannot cleanly undo. Describe the exact action and the target, then
  wait.
- **Reversible, read-only, and inspection actions proceed freely:**
  `get_app_state`, `capture_window`, `capture_screenshot`, `list_*`, scrolling,
  navigating, reading — look all you want.
- **Prefer the non-disturbing tier** so you never fight the user's live input,
  and **re-capture to confirm** what an element does before activating it. When a
  control's effect is ambiguous, treat it as destructive and ask.
- **Foreground input is doubly dangerous** — it types/clicks into whatever is
  focused, which may not be the app you think. Verify the frontmost app first.

## Two tiers of control — pick the right one

### NON-DISTURBING (default) — background, no focus steal

Operate on a **target app** by name/bundle-id/pid. The daemon captures that
app's window even when it's occluded or not frontmost, and acts through the
accessibility API. The user's foreground app and cursor are never touched.

| Verb | What it does |
|---|---|
| `get_app_state(app)` | Background window screenshot + indexed, actionable a11y tree. Read `text`, pick an element. Caches the snapshot. `screenshot["annotated_path"]` has every element's box and index drawn on it. `interactive_only=True` drops elements you cannot act on; `diff=True` adds what changed since the last snapshot. |
| `find(app, label=, role=)` | Elements in the last snapshot by label (exact first, then substring) and role. |
| `click(app, element_index)` or `click(app, label="Save")` | Press the element in the background. Add `role="button"` if the label is ambiguous. `method="foreground"` to force a real click instead. |
| `set_value(app, element_index or label=, value=)` | Set a text field/area's value directly — no keystrokes. Works on background windows. |
| `perform_action(app, element_index or label=, action=)` | Any AX action: `AXPress`, `AXShowMenu`, `AXIncrement`, `AXConfirm`, … |
| `wait_for(app, label=, role=, timeout=10)` | Re-snapshot until the element appears; returns it. Use instead of `sleep()`. |
| `wait_window(title, timeout=10)` | Wait for a window whose title contains `title`. |
| `capture_window(app=… or window_id=…)` | Background capture of a single window. |
| `list_windows(app=None)` | On-screen windows with id, owner, pid, bounds. |

Works reliably on standard AppKit apps (Notes, Mail, Safari, Finder, TextEdit,
System Settings, most native apps). Mac Catalyst apps (Calculator, Messages) and
GPU/canvas apps (games, some Electron canvases) expose few AX actions — use the
foreground tier for those.

### FOREGROUND (explicit) — global input, interrupts the user

Global CGEvent synthesis: moves the real cursor and drives whatever is frontmost.
Use only when the target has no usable accessibility tree.

| Verb | What it does |
|---|---|
| `capture_screenshot(region=None)` | Full-screen (or region) grab, logical points, 1:1 with click coords. |
| `record_screen(seconds=5)` | Record the desktop to an `.mov` for a fixed duration; returns the finished file. |
| `start_recording(name=…)` / `stop_recording(name=…)` | Open-ended desktop recording: start, do work, stop to finalize. `recording_status()` lists active ones. |
| `click_at_xy(x, y)` / `double_click_at_xy` / `right_click_at_xy` | Real cursor click. |
| `type_text(text)` | Type unicode into the focused field. |
| `press_key("cmd+shift+t")` | Key chords + named keys (`Return`, `Escape`, `down`, `Control_L+a`). |
| `drag(fx, fy, tx, ty)` · `scroll(dx, dy)` · `move_mouse(x, y)` | |
| `activate_app(app)` | Bring an app to the front (changes focus). |

Note: `capture_screenshot`, `record_screen`, and `start/stop_recording` are
*passive* — full-screen but no input and no focus change, so they don't disturb
the user. They live here only because they use the global display, not a target
window. The *input* verbs below `activate_app` are the disruptive ones.

Every element has `kind`, the same on macOS and Windows (`button`, `text`,
`checkbox`, `radio`, `link`, `menuitem`, `tab`, `listitem`, …), next to the
native `role` (`AXButton`, `ButtonControl`). Use `kind` in `role=` filters so the
same code runs on both. The `text` tree shows kinds.

## Coordinates

Everything is in **logical points**, and screenshots are downscaled so 1 image
pixel == 1 point — read a pixel off the image and click it directly, no Retina
math. Each element in `get_app_state` carries `center` (global points, for
`click_at_xy` / full-screen shots) and `rel` (window-relative, matching the
`get_app_state` window screenshot).

## What actually works

- **See first, then act.** `get_app_state(app)["text"]` is the numbered tree;
  its `screenshot.path` is the background window image. Read the path to look at
  it, pick an `element_index`, and `click(app, element_index)`.
- **Prefer AX over pixels.** `click(app, element_index)` and `set_value(...)` are
  robust to layout shifts and don't disturb the user. Drop to `click_at_xy` only
  when there's no accessibility node.
- **Verify by re-capturing.** After an action, `capture_window(app=…)` and read
  it to confirm the new state — same discipline as browser-harness screenshots.
- **Bulk/parallel:** the daemon is single-process; drive one app per heredoc.
  For several apps, run several heredocs.

## Evidence helpers (screen-recording / visual verification)

For UI/UX evidence (screen-recording / visual verification), extra helpers live
in `agent-workspace/agent_helpers.py`:

```bash
computer-harness <<'PY'
import sys; sys.path.insert(0, "~/tools/computer-harness/agent-workspace".replace("~", __import__("os").path.expanduser("~")))
from agent_helpers import evidence_shot, FlowRecorder, pixel_diff
d = "./evidence"
fr = FlowRecorder("compose-mail", d, app="Mail")   # background window frames
fr.frame()
click("Mail", element_index=3)
fr.frame()
print(fr.save_gif())                                # animated GIF, no ffmpeg
PY
```

`evidence_shot(label, dir, app=…)` writes a labeled PNG; `FlowRecorder` builds a
GIF "video" of a flow; `pixel_diff(before, after)` tints what changed.

## Commands

```
computer-harness --doctor     diagnose install, daemon, and TCC permissions
computer-harness --status     daemon + permission status as JSON
computer-harness --restart    stop the daemon (next call starts fresh, picks up code changes)
computer-harness mcp          serve the helpers as MCP tools (see install.md)
computer-harness --help
```

## Design constraints (mirrors browser-harness)

- Non-disturbing by default. The whole point is that the user keeps working.
- Public Apple APIs only — no private entitlements, no touching OpenAI's signed
  service. CGEvent / CGWindowList / AXUIElement.
- Connect to the running desktop; don't spawn a second display.
- Core helpers stay thin. Task-specific additions go in
  `agent-workspace/agent_helpers.py`, not the core package.
- No manager layer: no retry framework, no session manager, no config system.

## Gotchas (field-tested)

- **Catalyst apps ignore background AX/CGEvent** (Calculator, Messages). They
  need the foreground tier, or bring them front with `activate_app` first.
- **Background AX titles can be sparse.** Some apps only fully populate a11y
  attributes when frontmost; `role` + `value` + geometry still come through.
  `get_app_state` re-reads live each call, so re-snapshot after the app updates.
- **`element_index` is only valid against the last `get_app_state(app)`.** Re-run
  it after the UI changes; indices are positional, not stable ids.
- **A denied TCC grant looks like "nothing happens".** `--doctor` reports which
  of Accessibility / Screen Recording is missing and what to grant.
- **`capture_window` needs the window on-screen** (not minimized to the Dock).
  Minimized windows aren't in the CGWindowList on-screen set.
- **Windows on another Space can't be captured.** `get_app_state` still reads
  their a11y tree (AX sees all Spaces) but the screenshot comes back `None`,
  because `CGWindowListCreateImage` only has backing-store pixels for the current
  Space. Move the window to the active Space, or drive it AX-only (blind).

## MCP server

`computer-harness mcp` exposes these helpers as MCP tools (stdio, or HTTPS on
the LAN with a bearer token). See install.md. Over MCP there is no heredoc:
call the tools directly. `get_app_state` returns the tree text plus the
annotated window image.

## Windows

Same helpers, same heredoc. What differs:

- **Apps are found by exe name or window title**: `get_app_state("notepad")`,
  `get_app_state("Calculator")` (UWP apps live in ApplicationFrameHost, so use
  the title). `bundle_id` in `list_apps()` is the exe path.
- **Native roles are UI Automation names**: `ButtonControl`, `EditControl`,
  `CheckBoxControl`, `MenuItemControl`, … (`kind` is the same as on macOS).
  Each element lists its `actions`:
  `Invoke`, `Toggle`, `Select`, `Expand`/`Collapse`, `DefaultAction`.
  `click(app, i)` tries them in that order; `perform_action` accepts these names
  and the AX aliases `AXPress` and `AXShowMenu`.
- **Coordinates are physical pixels** (the daemon is DPI aware). Screenshots are
  still 1:1 with click coordinates.
- **`cmd` in a chord means `ctrl`** (`press_key("cmd+c")` sends Ctrl+C). Use
  `win` for the Windows key.
- **Focus guard.** Some apps (classic Win32 buttons, checkboxes) bring
  themselves to the front when pressed through UIA. Background actions put the
  user's window back within ~150 ms and report `focus_restored: True`. No
  guard (and no delay) when the target app is already in front. Keys the
  user types in that moment can land in the target app.
- **Background click fallback is weak.** Without a UIA action, `click` posts
  window messages, which only classic Win32 controls honour. Chromium, Electron
  and UWP content need `method="foreground"`.
- **`capture_window` fails on minimized windows.** Covered windows are fine.
- **Apps running as admin are out of reach** unless the daemon also runs
  elevated. `permissions()["elevated"]` tells you which.
- **Needs an unlocked, signed-in desktop.** Not a service, not an SSH session,
  not while a UAC prompt or the lock screen shows.
- **Recording needs ffmpeg on PATH** and writes `.mp4`. `display` and
  `show_clicks` are ignored; `audio=True` raises.

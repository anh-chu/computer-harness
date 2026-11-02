"""Platform-neutral tests: run on Linux, macOS and Windows with `pytest tests`."""
import asyncio
import json
import os
import threading
import time

import pytest
from PIL import Image

from computer_harness import common


def el(index, role, label="", value="", actions=(), settable=False, depth=0, rel=(20, 20)):
    return {"index": index, "role": role, "kind": common.kind_of(role), "label": label,
            "value": value, "actions": list(actions), "settable": settable, "depth": depth,
            "rel": list(rel), "w": 30, "h": 12}


TREE = [
    el(0, "AXWindow", "Doc"),
    el(1, "AXButton", "Save", actions=["AXPress"], depth=1),
    el(2, "ButtonControl", "Save as…", actions=["Invoke"], depth=1),
    el(3, "AXTextField", "", value="hello", settable=True, depth=1),
    el(4, "AXStaticText", "Save", depth=1),
]


# --- chords -----------------------------------------------------------------
WIN_MODS = {"control", "shift", "alt", "win"}
WIN_ALIASES = {"cmd": "control", "command": "control", "option": "alt", "super": "win"}


@pytest.mark.parametrize("chord, mods, key", [
    ("ctrl+shift+t", ["control", "shift"], "t"),
    ("cmd+c", ["control"], "c"),
    ("Return", [], "return"),
    ("esc", [], "escape"),
    ("Control_L+a", ["control"], "a"),
    ("super+e", ["win"], "e"),
    ("ctrl+ctrl+x", ["control"], "x"),
])
def test_parse_chord(chord, mods, key):
    assert common.parse_chord(chord, WIN_MODS, WIN_ALIASES) == (mods, key)


def test_parse_chord_needs_a_key():
    with pytest.raises(ValueError):
        common.parse_chord("ctrl+shift", WIN_MODS, WIN_ALIASES)


def test_norm_button():
    assert [common.norm_button(b) for b in ("R", "middle", None, "x")] == ["right", "middle", "left", "left"]


# --- kinds, lookup ----------------------------------------------------------

def test_kind_of_maps_both_platforms():
    assert common.kind_of("AXButton") == common.kind_of("ButtonControl") == "button"
    assert common.kind_of("AXTextField") == common.kind_of("EditControl") == "text"
    assert common.kind_of("SomethingNew") == "other"


def test_find_exact_before_substring():
    assert [e["index"] for e in common.find_elements(TREE, "save")] == [1, 4]
    assert [e["index"] for e in common.find_elements(TREE, "save as")] == [2]
    assert [e["index"] for e in common.find_elements(TREE, "hell")] == [3]  # matches value
    assert [e["index"] for e in common.find_elements(TREE, None, "text")] == [3]


def test_resolve_index_prefers_the_actionable_match():
    # "Save" hits the button and a static label; only the button acts.
    assert common.resolve_index(TREE, label="Save") == 1
    assert common.resolve_index(TREE, label="save", role="label") == 4
    assert common.resolve_index(TREE, element_index=3) == 3


def test_resolve_index_ambiguous_and_missing():
    two = TREE + [el(5, "AXButton", "Save", actions=["AXPress"])]
    with pytest.raises(ValueError, match="2 elements match"):
        common.resolve_index(two, label="Save", role="button")
    with pytest.raises(ValueError, match="no element"):
        common.resolve_index(TREE, label="Delete")
    with pytest.raises(ValueError):
        common.resolve_index(TREE)


# --- snapshot shaping -------------------------------------------------------

def test_render_text_indents_and_marks():
    lines = common.render_text(TREE).splitlines()
    assert lines[0] == "[0] window “Doc”"
    assert lines[1] == "  [1] button “Save”  • press"
    assert lines[3] == "  [3] text = hello  • settable"


def test_interactive_only_keeps_indices():
    out = common.shape_state({"elements": TREE, "text": ""}, interactive_only=True)
    assert [e["index"] for e in out["elements"]] == [1, 2, 3]
    assert "[3]" in out["text"] and "[0]" not in out["text"]


def test_diff_ignores_index_shifts():
    before = {"elements": TREE}
    after = {"elements": [TREE[0], el(1, "AXButton", "Open", actions=["AXPress"])] + TREE[1:]}
    d = common.shape_state(after, before, diff=True)["diff"]
    assert d["added"] == ["[·] button “Open”  • press"]
    assert d["removed"] == []
    assert common.shape_state(after, None, diff=True)["diff"]["first_snapshot"] is True


def test_annotate_draws_boxes(tmp_path):
    src, dst = tmp_path / "w.png", tmp_path / "w-marked.png"
    Image.new("RGB", (100, 60), "white").save(src)
    common.annotate(str(src), TREE[:2], str(dst))
    img = Image.open(dst)
    assert img.size == (100, 60)
    assert img.getpixel((5, 14)) != (255, 255, 255)  # box edge of element at rel (20,20), 30x12


# --- retention --------------------------------------------------------------

def test_prune_by_count_and_age(tmp_path):
    now = time.time()
    for i in range(5):
        p = tmp_path / f"{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (now - i * 3600, now - i * 3600))  # 0 newest .. 4 oldest
    assert common.prune(tmp_path, keep_files=3, keep_days=0, now=now) == 2
    assert sorted(os.listdir(tmp_path)) == ["0.png", "1.png", "2.png"]
    assert common.prune(tmp_path, keep_files=0, keep_days=1.5 / 24, now=now) == 1
    assert sorted(os.listdir(tmp_path)) == ["0.png", "1.png"]
    assert common.prune(tmp_path / "missing", 1, 1) == 0


# --- IPC round trip (AF_UNIX on POSIX, loopback + token on Windows) ---------

def test_ipc_round_trip(tmp_path, monkeypatch):
    from computer_harness import ipc
    monkeypatch.setattr(ipc, "_RUNTIME", tmp_path if ipc.WINDOWS else __import__("pathlib").Path("/tmp"))
    name = f"pytest{os.getpid()}"

    async def handler(reader, writer):
        req = json.loads(await reader.readline())
        resp = {"pong": True} if req.get("meta") == "ping" else {"echo": req}
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        writer.close()

    loop = asyncio.new_event_loop()
    server = loop.create_task(ipc.serve(name, handler))
    t = threading.Thread(target=lambda: loop.run_until_complete(
        asyncio.gather(server, return_exceptions=True)), daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        while not ipc.ping(name) and time.time() < deadline:
            time.sleep(0.05)
        assert ipc.ping(name)
        assert ipc.call({"op": "x", "args": {"n": "ü"}}, name=name) == {"echo": {"op": "x", "args": {"n": "ü"}}}
    finally:
        loop.call_soon_threadsafe(server.cancel)
        t.join(5)
        loop.close()
        ipc.cleanup(name)

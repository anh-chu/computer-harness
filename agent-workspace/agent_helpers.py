"""Task-specific extensions for computer-harness. Mirrors browser-harness's
agent_helpers: labeled evidence shots, a no-ffmpeg GIF flow recorder, and a
pixel-diff. Import in a heredoc via:

    import sys, os; sys.path.insert(0, os.path.expanduser("~/tools/computer-harness/agent-workspace"))
    from agent_helpers import evidence_shot, FlowRecorder, pixel_diff
"""
import os
import time

from computer_harness.helpers import capture_screenshot, capture_window


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def evidence_shot(label, out_dir, app=None, window_id=None, region=None):
    """Capture a labeled PNG into out_dir. If app/window_id given, does a
    NON-DISTURBING background window capture; else a full-screen (or region)
    grab. Returns the file path."""
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:60]
    path = os.path.join(out_dir, f"{_stamp()}-{safe}.png")
    if app or window_id:
        capture_window(app=app, window_id=window_id, path=path)
    else:
        capture_screenshot(path=path, region=region)
    return path


class FlowRecorder:
    """Record a multi-step flow as an animated GIF (no ffmpeg; Pillow only).

    fr = FlowRecorder("login", "/path/evidence", app="Safari")
    fr.frame()            # capture current state
    ... drive the app ...
    fr.frame()
    gif = fr.save_gif()   # returns gif path
    """

    def __init__(self, name, out_dir, app=None, window_id=None, region=None, fps=2):
        self.name = name
        self.out_dir = out_dir
        self.app = app
        self.window_id = window_id
        self.region = region
        self.fps = fps
        self.frames = []
        os.makedirs(out_dir, exist_ok=True)

    def frame(self, label=None):
        p = evidence_shot(label or f"{self.name}-{len(self.frames):03d}",
                          self.out_dir, app=self.app, window_id=self.window_id,
                          region=self.region)
        self.frames.append(p)
        return p

    def save_gif(self, path=None, loop=0):
        from PIL import Image
        if not self.frames:
            raise ValueError("no frames captured")
        path = path or os.path.join(self.out_dir, f"{self.name}-{_stamp()}.gif")
        imgs = [Image.open(f).convert("RGB") for f in self.frames]
        w = min(i.width for i in imgs)
        imgs = [i.resize((w, int(i.height * w / i.width))) for i in imgs]
        imgs[0].save(path, save_all=True, append_images=imgs[1:],
                     duration=int(1000 / self.fps), loop=loop, optimize=True)
        return path


def pixel_diff(before_path, after_path, out_path=None, tint=(255, 0, 128)):
    """Tint changed pixels between two PNGs; returns (out_path, changed_fraction)."""
    from PIL import Image, ImageChops
    a = Image.open(before_path).convert("RGB")
    b = Image.open(after_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b).convert("L")
    mask = diff.point(lambda p: 255 if p > 24 else 0)
    changed = sum(mask.point(lambda p: 1 if p else 0).getdata()) / (a.width * a.height)
    overlay = Image.new("RGB", a.size, tint)
    out = Image.composite(overlay, b, mask)
    out_path = out_path or (os.path.splitext(after_path)[0] + "-diff.png")
    out.save(out_path)
    return out_path, round(changed, 4)

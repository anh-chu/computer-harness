"""computer-harness — native macOS and Windows computer use for agents.

Direct OS control: CoreGraphics / screencapture / AXUIElement on macOS
(engine.py), SendInput / PrintWindow / UI Automation on Windows (engine_win.py). Sibling to browser-harness: same
daemon + heredoc shape, but drives arbitrary native apps, not just Chrome.
"""
__version__ = "0.2.0"

"""Top-center floating overlay: 🎤 + real-time waveform + transcription text.

Style per design §6.1: a "波形胶囊".
- Starts hidden.
- Shown by the app on recording start.
- Waveform reflects the latest microphone RMS bars.
- Text label shows the partial transcription result.
- Never accepts focus (the user's input field must keep focus).
- Auto-hides on recording end.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gdk, Gtk  # type: ignore

logger = logging.getLogger(__name__)

OVERLAY_WIDTH = 520
OVERLAY_HEIGHT = 64
WAVE_BARS = 24           # number of bars in the scrolling waveform
WAVE_BAR_W = 4           # width per bar
WAVE_BAR_GAP = 3         # gap between bars
WAVE_BAR_MAX_H = 24      # max bar height (px)


class Overlay:
    """The floating 波形胶囊."""

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._label: Optional[Gtk.Label] = None
        self._wave: Optional[Gtk.DrawingArea] = None
        self._bars: deque[float] = deque([0.0] * WAVE_BARS, maxlen=WAVE_BARS)
        self._redraw_src: int | None = None
        self._status_text: str = ""
        self._text: str = ""
        self._visible: bool = False

    # ---- public API ----

    def show(self, status: str = "聆听中…") -> None:
        self._ensure_window()
        self._status_text = status
        self._refresh_label()
        if not self._visible:
            self._window.set_visible(True)
            self._visible = True
        self._arm_redraw()

    def hide(self) -> None:
        if self._window and self._visible:
            self._window.set_visible(False)
            self._visible = False
        if self._redraw_src is not None:
            try:
                GLib.source_remove(self._redraw_src)
            except Exception:
                pass
            self._redraw_src = None

    def set_text(self, text: str) -> None:
        self._text = text
        if self._window:
            self._refresh_label()

    def set_status(self, status: str) -> None:
        self._status_text = status
        if self._window:
            self._refresh_label()

    def push_rms(self, rms: float) -> None:
        self._bars.append(max(0.0, min(1.0, rms)))
        # redraw is driven by the idle timer; no need to queue directly

    # ---- internals ----

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        win = Gtk.Window()
        win.set_title("doubao-input overlay")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        # Critical: keep focus with the user's input field, not us.
        win.set_focus_on_click(False)
        win.set_can_focus(False)
        # NOTE: GTK4 removed set_skip_taskbar_hint / set_skip_pager_hint
        # / set_keep_above. The overlay is a regular borderless window.

        # Try layer-shell / overlay if available; harmless no-op otherwise.
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                surface = Gdk.Surface.new_toplevel(display)  # just to test
        except Exception:
            pass

        # Center horizontally, near the top.
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    mon = monitors.get_item(0)
                    geo = mon.get_geometry()
                    x = geo.x + (geo.width - OVERLAY_WIDTH) // 2
                    y = geo.y + 24
                    # GTK4 has no set_position; the compositor places us.
                    # We just make sure we're not 0,0.
                    win.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
                    _ = (x, y)  # hint only
        except Exception:
            pass

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        # Waveform drawing area (left)
        wave = Gtk.DrawingArea()
        wave.set_size_request(WAVE_BARS * (WAVE_BAR_W + WAVE_BAR_GAP) + 4, 40)
        wave.set_draw_func(self._draw_wave)
        self._wave = wave
        box.append(wave)

        # Text label (right)
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_yalign(0.5)
        label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        label.set_max_width_chars(40)
        label.set_single_line_mode(True)
        self._label = label
        box.append(label)

        win.set_child(box)
        self._window = win

    def _refresh_label(self) -> None:
        if not self._label:
            return
        if self._text:
            self._label.set_text(self._text)
        else:
            self._label.set_text(self._status_text)

    def _arm_redraw(self) -> None:
        if self._redraw_src is not None:
            return
        self._redraw_src = GLib.timeout_add(60, self._tick_redraw)

    def _tick_redraw(self) -> bool:
        if not self._visible or not self._wave:
            self._redraw_src = None
            return False
        self._wave.queue_draw()
        return True  # keep running

    def _draw_wave(self, area, cr, w, h) -> None:
        # Dark semi-transparent background with rounded corners
        try:
            cr.set_source_rgba(0.10, 0.10, 0.12, 0.92)
            radius = 18.0
            _rounded_rect(cr, 0, 0, w, h, radius)
            cr.fill()
        except Exception:
            pass

        bars = list(self._bars)
        n = len(bars)
        if n == 0:
            return
        cx0 = (w - n * (WAVE_BAR_W + WAVE_BAR_GAP)) / 2
        for i, v in enumerate(bars):
            bh = max(2.0, v * WAVE_BAR_MAX_H)
            x = cx0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
            y = (h - bh) / 2
            # Soft gradient look: alpha based on height
            alpha = 0.55 + 0.45 * v
            cr.set_source_rgba(0.45, 0.85, 0.55, alpha)
            _rounded_rect(cr, x, y, WAVE_BAR_W, bh, 1.5)
            cr.fill()


def _rounded_rect(cr, x, y, w, h, r) -> None:
    if w <= 0 or h <= 0:
        return
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()

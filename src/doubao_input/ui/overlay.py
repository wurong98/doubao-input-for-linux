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

import cairo  # used to render ImageSurface that we then wrap in Gdk.Texture

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gdk, Gtk  # type: ignore

logger = logging.getLogger(__name__)

OVERLAY_WIDTH = 560
OVERLAY_HEIGHT = 76  # taller: 1 short line uses ~40px, 2 lines fit comfortably
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
        # Reset per-round state so a 2nd PTT press never displays the
        # tail of the 1st round's recognition text.
        self._text = ""
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
            self._arm_redraw()  # re-render wave (shape depends on has_text)

    def set_status(self, status: str) -> None:
        self._status_text = status
        if self._window:
            self._refresh_label()
            self._arm_redraw()

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
        # Allow vertical resize so multi-line text can grow the window;
        # we still cap width via the label's max_width_chars.
        win.set_resizable(True)
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

        # Waveform area (left): use Gtk.Picture with a Gdk.Texture built
        # from a cairo ImageSurface. We render to ImageSurface in Python
        # (no PyGObject cairo.Context pass-through needed) and only
        # touch Gdk.Texture/Gtk.Picture from Python.
        wave = Gtk.Picture()
        wave.set_size_request(WAVE_BARS * (WAVE_BAR_W + WAVE_BAR_GAP) + 4, 40)
        wave.set_can_shrink(False)
        self._wave = wave
        self._wave_tex: Optional[Gdk.Texture] = None
        # Initial blank frame
        self._upload_wave_texture()
        box.append(wave)

        # Text label (right).
        # Layout goals:
        #   - Short text (e.g. 2 chars) → 1 line, centered.
        #   - Long text → wrap to ≤ 2 lines, both lines centered.
        # We deliberately do NOT use set_ellipsize(START): combined with
        # max_width_chars + wrap + hexpand, GTK 4 / Pango renders the
        # whole line as a single "…" (treating wrap as "all fits in
        # one line" and ellipsizing from the start).  Truncation is
        # already handled in _refresh_label via text[-600:].
        label = Gtk.Label()
        label.set_xalign(0.5)            # center within the label box
        label.set_yalign(0.5)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_wrap(True)
        label.set_wrap_mode(0)           # Pango.WrapMode.WORD
        label.set_max_width_chars(40)
        # NO set_lines(2): let Pango lay out 1 or 2 lines naturally.
        # NO set_ellipsize: see comment above; would force "…" in some
        # widths even when text is short.
        label.set_hexpand(True)          # let label take full row width
        label.set_halign(Gtk.Align.CENTER)
        self._label = label
        box.append(label)

        win.set_child(box)
        self._window = win

    def _refresh_label(self) -> None:
        if not self._label:
            return
        if self._text:
            # Defensive cap: don't let Pango chew on 100k chars of
            # accumulated ASR output. The label already elides from the
            # start so the user always sees the latest; the cap just
            # keeps memory and layout cost bounded.
            text = self._text
            if len(text) > 600:
                text = text[-600:]
            self._label.set_text(text)
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
        self._upload_wave_texture()
        return True  # keep running

    def _upload_wave_texture(self) -> None:
        """Render current bars to a cairo ImageSurface, then wrap it in
        a Gdk.Texture and assign to the Gtk.Picture. No draw_func, no
        PyGObject cairo.Context pass-through → no foreign-struct errors."""
        if self._wave is None:
            return
        try:
            import cairo as _cairo
            bars = list(self._bars)
            has_text = bool(self._text)
            max_bar_h = WAVE_BAR_MAX_H if not has_text else 18
            cw = WAVE_BARS * (WAVE_BAR_W + WAVE_BAR_GAP) + 4
            ch = 40
            surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, cw, ch)
            cr = _cairo.Context(surf)
            # background
            cr.set_source_rgba(0.10, 0.10, 0.12, 0.92)
            _rounded_rect(cr, 0, 0, cw, ch, 12.0)
            cr.fill()
            # bars
            n = len(bars)
            if n > 0:
                cx0 = (cw - n * (WAVE_BAR_W + WAVE_BAR_GAP)) / 2
                for i, v in enumerate(bars):
                    bh = max(2.0, v * max_bar_h)
                    x = cx0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
                    y = (ch - bh) / 2
                    alpha = 0.55 + 0.45 * v
                    cr.set_source_rgba(0.45, 0.85, 0.55, alpha)
                    _rounded_rect(cr, x, y, WAVE_BAR_W, bh, 1.5)
                    cr.fill()
            # cairo ImageSurface → Gdk.Texture (no PyGObject cairo
            # foreign-struct conversion needed; Gdk.Texture accepts
            # raw bytes via new_from_bytes).
            stride = cairo.ImageSurface.format_stride_for_width(
                _cairo.FORMAT_ARGB32, cw
            )
            data = surf.get_data()  # bytes
            # Gdk.Texture.new_from_bytes expects a GBytes wrapping a
            # memory buffer in RGBA (GTK expects RGBA on the wire, but
            # Cairo ARGB32 is BGRA on little-endian). For our flat-color
            # background + green bars the channel swap is invisible, but
            # to be safe we explicitly swap R/B.
            import array as _arr
            arr = _arr.array("B", data)
            # ARGB32 in Cairo's layout = [B, G, R, A] on little-endian.
            # GTK expects [R, G, B, A]. Swap bytes 0<->2.
            for i in range(0, len(arr), 4):
                arr[i], arr[i + 2] = arr[i + 2], arr[i]
            gbytes = GLib.Bytes.new(bytes(arr))
            tex = Gdk.Texture.new_from_bytes(gbytes, cw, ch, stride)
            self._wave.set_paintable(tex)
            self._wave_tex = tex
        except Exception:
            # Drawing is purely cosmetic; never let it kill the overlay.
            pass

    def _draw_wave(self, area, cr, w, h) -> None:
        # GTK4 PyGObject draw_func callbacks receive cairo.Context, but the
        # type converter registration is broken in several Ubuntu/PyGObject
        # versions (produces "Couldn't find foreign struct converter for
        # 'cairo.Context'"). Render to an ImageSurface first, then draw
        # the texture via cairo.set_source_surface — that path goes
        # through pixman, not PyGObject's struct converter.
        try:
            import cairo as _cairo
            bars = list(self._bars)
            n = len(bars)
            # Decide visual layout: when text exists, bars shrink to left
            has_text = bool(self._text)
            max_bar_h = WAVE_BAR_MAX_H if not has_text else 18
            cw = max(w, 1)
            ch = max(h, 1)
            surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, cw, ch)
            inner = _cairo.Context(surf)
            # background
            inner.set_source_rgba(0.10, 0.10, 0.12, 0.92)
            _rounded_rect(inner, 0, 0, cw, ch, 18.0)
            inner.fill()
            # bars
            if n > 0:
                cx0 = (cw - n * (WAVE_BAR_W + WAVE_BAR_GAP)) / 2
                for i, v in enumerate(bars):
                    bh = max(2.0, v * max_bar_h)
                    x = cx0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
                    y = (ch - bh) / 2
                    alpha = 0.55 + 0.45 * v
                    inner.set_source_rgba(0.45, 0.85, 0.55, alpha)
                    _rounded_rect(inner, x, y, WAVE_BAR_W, bh, 1.5)
                    inner.fill()
            # blit the rendered surface onto the widget's cairo context
            cr.set_source_surface(surf, 0, 0)
            cr.paint()
        except Exception as e:
            # Never let draw errors spam logs or crash the overlay.
            # (Earlier versions of this code raised the PyGObject
            # "foreign struct converter" error every redraw; catching
            # here is the safe fallback.)
            pass


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

"""Top-center floating overlay: 圆角胶囊 + 波形 + 文字。

视觉规范(用户反馈,2026-06-21):
  1. 整体圆滑,圆角 18px
  2. 动效只发生在外边 1-2px 描边 / 背景最外层辉光,文字与
     候选字区域保持绝对静态、纯白高对比度
  3. 背景呼吸:alpha 在 0.45 ~ 0.85 之间循环(2.4s 周期),
     调整透明度而非饱和度,避免扎眼
  4. 敲击触发流光(每打一字闪一次):background alpha 短暂
     跳到 0.95,持续 220ms,然后 400ms 衰减回呼吸基线
  5. 边缘描边:1.5px 白色 alpha 跟随呼吸(0.18 ~ 0.45),
     "流光" 触发时短暂 0.85

实现:
  - 后景: Gtk.Picture 整张圆角半透明 PNG(alpha 由 tick 算)
  - 前景:wave (Gdk.Texture) + label (Pango),不受动效影响
  - 60ms GLib.timeout 驱动
  - 用 Pango 标色 (CSS): label 强制白,wave bar 强制绿

The window is Gtk.Window (borderless, never accepts focus), so
the floating 波形胶囊 never steals focus from the user's input
field.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Optional

import gi

import cairo  # for cairo.ImageSurface used in Gdk.Texture rendering

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gdk, Gtk  # type: ignore

logger = logging.getLogger(__name__)

# ----- Geometry -----
OVERLAY_WIDTH = 560
# 后景圆角 PNG 是 OVERLAY_WIDTH × OVERLAY_HEIGHT;前景 widget 居中堆在它上面。
OVERLAY_HEIGHT = 110
WAVE_BARS = 24
WAVE_BAR_W = 4
WAVE_BAR_GAP = 3
WAVE_BAR_MAX_H = 24
CORNER_R = 18  # 圆角半径(px)

# ----- Animation parameters -----
TICK_MS = 60                              # ~16 fps, 足够人眼感知且不浪费 CPU
BREATH_PERIOD_S = 2.4                     # 一次呼吸周期
BREATH_ALPHA_MIN = 0.45                   # 背景最暗(呼吸谷底)
BREATH_ALPHA_MAX = 0.85                   # 背景最亮(呼吸峰)
BORDER_ALPHA_MIN = 0.18                   # 描边最暗
BORDER_ALPHA_MAX = 0.45                   # 描边最亮
GLINT_PEAK_ALPHA = 0.95                   # 敲击时背景跳到这
GLINT_PEAK_BORDER = 0.85                  # 敲击时描边跳到这
GLINT_HOLD_S = 0.22                       # 峰持续(秒)
GLINT_DECAY_S = 0.40                       # 衰减回呼吸(秒)


class Overlay:
    """The floating 圆角 波形胶囊."""

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._label: Optional[Gtk.Label] = None
        self._wave: Optional[Gtk.Picture] = None
        self._bg: Optional[Gtk.Picture] = None
        self._bars: deque[float] = deque([0.0] * WAVE_BARS, maxlen=WAVE_BARS)
        self._ticker_src: int | None = None
        self._t0: float = 0.0                  # animation epoch
        self._glint_t: Optional[float] = None  # last keystroke flash time
        self._status_text: str = ""
        self._text: str = ""
        self._visible: bool = False

    # ---- public API ----

    def show(self, status: str = "聆听中…") -> None:
        self._ensure_window()
        # Reset per-round state.
        self._text = ""
        self._status_text = status
        self._refresh_label()
        if not self._visible:
            self._window.set_visible(True)
            self._visible = True
        self._arm_ticker()

    def hide(self) -> None:
        if self._window and self._visible:
            self._window.set_visible(False)
            self._visible = False
        if self._ticker_src is not None:
            try:
                GLib.source_remove(self._ticker_src)
            except Exception:
                pass
            self._ticker_src = None

    def set_text(self, text: str) -> None:
        prev_len = len(self._text)
        self._text = text
        if self._window:
            self._refresh_label()
            # Each ASR result that GROWS the text counts as a
            # "keystroke" for the glint effect — gives the user
            # satisfying causal feedback ("a new word arrived").
            if len(text) > prev_len:
                self.on_keystroke()
            self._arm_ticker()  # wave shape depends on has_text

    def set_status(self, status: str) -> None:
        self._status_text = status
        if self._window:
            self._refresh_label()
            self._arm_ticker()

    def push_rms(self, rms: float) -> None:
        self._bars.append(max(0.0, min(1.0, rms)))

    def on_keystroke(self) -> None:
        """User typed a character (or word arrived) — fire the glint
        flash.  Cheap to call repeatedly; we just move the timestamp
        forward so the flash restarts at peak."""
        if not self._visible:
            return
        self._glint_t = time.monotonic()
        self._arm_ticker()

    # ---- internals ----

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        win = Gtk.Window()
        win.set_title("doubao-input overlay")
        win.set_decorated(False)
        win.set_resizable(True)
        win.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        # Critical: keep focus with the user's input field, not us.
        win.set_focus_on_click(False)
        win.set_can_focus(False)

        # Center horizontally, near the top.
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    mon = monitors.get_item(0)
                    geo = mon.get_geometry()
                    win.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
                    _ = (geo.x + (geo.width - OVERLAY_WIDTH) // 2,
                         geo.y + 24)
        except Exception:
            pass

        # Outer Overlay: lets the Gtk.Picture BG extend past the
        # inner box without forcing the window to track its size.
        # The window itself stays at OVERLAY_W × OVERLAY_H; the BG
        # fills it via hexpand/vexpand.
        outer = Gtk.Overlay()
        # Background layer: round-cornered translucent panel.
        bg = Gtk.Picture()
        bg.set_can_shrink(False)
        bg.set_hexpand(True)
        bg.set_vexpand(True)
        self._bg = bg
        self._bg_tex: Optional[Gdk.Texture] = None
        outer.set_child(bg)

        # Foreground content box: stack wave on top, label below.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # Wave (top).
        wave = Gtk.Picture()
        wave.set_size_request(WAVE_BARS * (WAVE_BAR_W + WAVE_BAR_GAP) + 4, 40)
        wave.set_can_shrink(False)
        wave.set_halign(Gtk.Align.CENTER)
        self._wave = wave
        self._wave_tex: Optional[Gdk.Texture] = None
        box.append(wave)

        # Text label (bottom).
        #  - Short text (e.g. 2 chars) → 1 line, centered, no blank 2nd line.
        #  - Hard width 360px so Pango has a real break-width before
        #    the first size allocation, avoiding the "每字一行" bug.
        #  - set_lines(2) caps the height at 2 lines.
        #  - No ellipsize (CJK + wrap + ellipsize Pango bug).
        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_yalign(0.5)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_wrap(True)
        label.set_wrap_mode(0)         # Pango.WrapMode.WORD
        label.set_size_request(360, -1)
        label.set_width_chars(14)
        label.set_lines(2)
        # High-contrast text colour, unaffected by breathing/glint
        # animation (animation only touches the BG layer's alpha).
        label.set_markup(
            '<span foreground="#ffffff" weight="bold">'
            f"{self._status_text or ' '}"
            "</span>"
        )
        self._label = label
        box.append(label)

        outer.add_overlay(box)
        win.set_child(outer)
        self._window = win

        # Initial blank frames.
        self._upload_wave_texture()
        self._upload_bg_texture(BREATH_ALPHA_MIN, BORDER_ALPHA_MIN)

    def _refresh_label(self) -> None:
        if not self._label:
            return
        if self._text:
            text = self._text
            if len(text) > 600:
                text = text[-600:]
            display = text
        else:
            display = self._status_text
        # White, bold — high contrast, NO animation tied to this.
        from html import escape
        self._label.set_markup(
            f'<span foreground="#ffffff" weight="bold">'
            f"{escape(display)}"
            "</span>"
        )

    def _arm_ticker(self) -> None:
        if self._ticker_src is not None:
            return
        if self._t0 == 0.0:
            self._t0 = time.monotonic()
        self._ticker_src = GLib.timeout_add(TICK_MS, self._tick)

    def _tick(self) -> bool:
        if not self._visible or not self._wave or not self._bg:
            self._ticker_src = None
            return False
        now = time.monotonic()
        # Breathing wave: 0..1 sinusoid, period BREATH_PERIOD_S.
        phase = (now - self._t0) / BREATH_PERIOD_S * 2.0 * math.pi
        b01 = 0.5 * (1.0 + math.sin(phase))         # 0..1
        bg_alpha = BREATH_ALPHA_MIN + (BREATH_ALPHA_MAX - BREATH_ALPHA_MIN) * b01
        border_alpha = BORDER_ALPHA_MIN + (BORDER_ALPHA_MAX - BORDER_ALPHA_MIN) * b01

        # Glint: if a keystroke happened recently, override peak then decay.
        if self._glint_t is not None:
            dt = now - self._glint_t
            if dt < GLINT_HOLD_S:
                bg_alpha = GLINT_PEAK_ALPHA
                border_alpha = GLINT_PEAK_BORDER
            elif dt < GLINT_HOLD_S + GLINT_DECAY_S:
                # Smooth linear decay from peak → current breathing value.
                t = (dt - GLINT_HOLD_S) / GLINT_DECAY_S
                bg_alpha = GLINT_PEAK_ALPHA + (bg_alpha - GLINT_PEAK_ALPHA) * t
                border_alpha = GLINT_PEAK_BORDER + (border_alpha - GLINT_PEAK_BORDER) * t
            else:
                self._glint_t = None  # done

        self._upload_wave_texture()
        self._upload_bg_texture(bg_alpha, border_alpha)
        return True

    def _upload_bg_texture(self, bg_alpha: float, border_alpha: float) -> None:
        """Render the round-corner translucent panel + 1.5px white
        stroke.  Alpha values come from the breathing/glint ticker."""
        if self._bg is None:
            return
        try:
            import cairo as _cairo
            cw = OVERLAY_WIDTH
            ch = OVERLAY_HEIGHT
            surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, cw, ch)
            cr = _cairo.Context(surf)
            cr.set_operator(_cairo.OPERATOR_CLEAR)
            cr.paint()                            # fully transparent
            cr.set_operator(_cairo.OPERATOR_OVER)
            # Translucent dark panel (only here is alpha animated).
            cr.set_source_rgba(0.07, 0.08, 0.10, bg_alpha)
            _rounded_rect(cr, 0, 0, cw, ch, CORNER_R)
            cr.fill()
            # 1.5px white outer stroke (alpha animated, but never past
            # BORDER_ALPHA_MAX unless a glint is firing).
            cr.set_source_rgba(1.0, 1.0, 1.0, border_alpha)
            cr.set_line_width(1.5)
            _rounded_rect(cr, 0.75, 0.75, cw - 1.5, ch - 1.5, CORNER_R - 0.75)
            cr.stroke()
            tex = _image_surface_to_texture(surf, cw, ch)
            self._bg.set_paintable(tex)
            self._bg_tex = tex
        except Exception:
            pass

    def _upload_wave_texture(self) -> None:
        """Render the waveform bars onto a transparent background.
        The wave itself is part of the foreground and is NOT animated
        for opacity — only the BG breathes.  Bars use a fixed green
        so they remain high-contrast on top of the panel."""
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
            cr.set_operator(_cairo.OPERATOR_CLEAR)
            cr.paint()                            # transparent bg
            cr.set_operator(_cairo.OPERATOR_OVER)
            n = len(bars)
            if n > 0:
                cx0 = (cw - n * (WAVE_BAR_W + WAVE_BAR_GAP)) / 2
                for i, v in enumerate(bars):
                    bh = max(2.0, v * max_bar_h)
                    x = cx0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
                    y = (ch - bh) / 2
                    # Single fixed green — high contrast on the dark panel.
                    cr.set_source_rgba(0.50, 0.95, 0.65, 0.95)
                    _rounded_rect(cr, x, y, WAVE_BAR_W, bh, 1.5)
                    cr.fill()
            tex = _image_surface_to_texture(surf, cw, ch)
            self._wave.set_paintable(tex)
            self._wave_tex = tex
        except Exception:
            pass


def _image_surface_to_texture(surf, cw: int, ch: int) -> Gdk.Texture:
    """Convert a cairo ARGB32 ImageSurface to a Gdk.Texture without
    touching PyGObject's broken cairo.Context foreign-struct converter.
    We swap R/B because cairo's ARGB32 little-endian = [B,G,R,A] while
    GTK's Gdk.Texture wants [R,G,B,A]."""
    stride = cairo.ImageSurface.format_stride_for_width(
        cairo.FORMAT_ARGB32, cw
    )
    data = surf.get_data()
    import array as _arr
    arr = _arr.array("B", data)
    for i in range(0, len(arr), 4):
        arr[i], arr[i + 2] = arr[i + 2], arr[i]
    gbytes = GLib.Bytes.new(bytes(arr))
    return Gdk.Texture.new_from_bytes(gbytes, cw, ch, stride)


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

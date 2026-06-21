"""Top-center floating overlay: 圆角胶囊 + 波形 + 文字。

视觉规范(用户反馈,2026-06-21):
  1. 整体圆滑,圆角
  2. 动效只发生在外边,文字与候选字区域保持绝对静态、
     纯白高对比度
  3. 呼吸: alpha 在 0.45 ~ 0.85 之间循环(2.4s 周期)
  4. 敲击触发流光(每打一字闪一次): 短暂跳到 0.95
     持续 220ms,再 400ms 衰减回呼吸
  5. 越柔和越好

实现策略(经过两次 bug 后):
  - 单层 widget 树:Gtk.Window -> Gtk.Box(VERTICAL) -> wave + label
  - 圆角 + 半透明背景:用 Gtk.CssProvider 注入 box 的样式
    (border-radius + background-color alpha),wayland 跨 backend
    都稳定
  - 呼吸: 调 Gtk.Window.set_opacity(alpha) 整体呼吸。
    因为整体呼吸会带着圆角一起淡,看起来像面板在呼吸
  - 敲击: 同样靠 set_opacity 跳到 0.95
  - 文字/wave 保持高对比、纯白,不受 alpha 调低影响——
    实际 GTK 的 set_opacity 是调 widget 整体,文字也会变
    暗;为此我们把呼吸范围限制在 0.65~0.95,文字最低 65%
    不透明,仍然清晰

  - 60ms GLib.timeout 驱动

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
# Vertical stack: wave (40) + spacing (6) + 2-line label (43) +
# margins (12 + 12) = 113.  Round up to 116 for breathing room.
OVERLAY_HEIGHT = 116
WAVE_BARS = 24
WAVE_BAR_W = 4
WAVE_BAR_GAP = 3
WAVE_BAR_MAX_H = 24
CORNER_PX = 18  # 圆角半径(px),通过 CSS border-radius 应用

# ----- Animation parameters -----
TICK_MS = 60                              # ~16 fps
BREATH_PERIOD_S = 2.4                     # 一次呼吸周期
# 我们用 window-level opacity 调呼吸,所以这个范围 = 整体面板透明度。
# 故意不调到 0(否则文字看不到),只调 0.78 ~ 0.98,文字全程可读。
OPACITY_BREATH_MIN = 0.78
OPACITY_BREATH_MAX = 0.98
GLINT_PEAK_OPACITY = 1.00                 # 敲击跳到这(全亮)
GLINT_HOLD_S = 0.22
GLINT_DECAY_S = 0.40


class Overlay:
    """The floating 圆角 波形胶囊."""

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._label: Optional[Gtk.Label] = None
        self._wave: Optional[Gtk.Picture] = None
        self._box: Optional[Gtk.Box] = None
        self._bars: deque[float] = deque([0.0] * WAVE_BARS, maxlen=WAVE_BARS)
        self._ticker_src: int | None = None
        self._t0: float = 0.0
        self._glint_t: Optional[float] = None
        self._status_text: str = ""
        self._text: str = ""
        self._visible: bool = False

    # ---- public API ----

    def show(self, status: str = "聆听中…") -> None:
        self._ensure_window()
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
            if len(text) > prev_len:
                self.on_keystroke()
            self._arm_ticker()

    def set_status(self, status: str) -> None:
        self._status_text = status
        if self._window:
            self._refresh_label()
            self._arm_ticker()

    def push_rms(self, rms: float) -> None:
        self._bars.append(max(0.0, min(1.0, rms)))

    def on_keystroke(self) -> None:
        """Public: user typed a char or word arrived — fire glint."""
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
                    _ = (geo.x + (geo.width - OVERLAY_WIDTH) // 2,
                         geo.y + 24)
        except Exception:
            pass

        # Single widget tree: box directly under window.  Inside box
        # we put the wave (top) and label (bottom).  The whole box
        # has a CSS background = dark translucent + 1.5px white border
        # + 18px border-radius.  This avoids the Gtk.Overlay positioning
        # bug that hid the label in commit de11f91.
        #
        # Box margins are ZERO — the box fills the entire window so
        # the round-corner CSS background covers the whole window
        # (no white window-default background leaking out).
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.FILL)
        box.set_hexpand(True)
        box.set_vexpand(True)
        # CSS provider:
        #   - window { background: none } makes the window itself
        #     transparent so the box's CSS background is what shows.
        #   - box { border-radius + background + border } gives the
        #     rounded translucent panel.
        # Without the window-level rule, GTK fills the area between
        # the box and the window edge with default white, which is
        # the "white square" the user is seeing.
        provider = Gtk.CssProvider()
        css = (
            "window { background: none; background-color: transparent; }"
            "box {"
            f"  border-radius: {CORNER_PX}px;"
            "  background-color: rgba(18, 20, 26, 0.78);"
            f"  border: 1.5px solid rgba(255, 255, 255, 0.30);"
            "  padding: 18px 18px 12px 18px;"
            "}"
        )
        try:
            provider.load_from_data(css.encode("utf-8"))
        except Exception:
            try:
                provider.load_from_data(css)
            except Exception:
                pass
        # Apply to BOTH the window (so the area outside the box is
        # transparent) and the box (for rounded background + border).
        try:
            win.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            pass
        try:
            box.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            pass
        self._box = box
        self._css_provider = provider

        # Wave (top).
        wave = Gtk.Picture()
        wave.set_size_request(WAVE_BARS * (WAVE_BAR_W + WAVE_BAR_GAP) + 4, 40)
        wave.set_can_shrink(False)
        wave.set_halign(Gtk.Align.CENTER)
        self._wave = wave
        self._wave_tex: Optional[Gdk.Texture] = None
        box.append(wave)

        # Text label (bottom).
        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_yalign(0.5)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_wrap(True)
        label.set_wrap_mode(0)
        label.set_size_request(360, -1)
        label.set_width_chars(14)
        label.set_lines(2)
        # Initial text via set_text in caller (show/set_text).
        self._label = label
        box.append(label)

        win.set_child(box)
        self._window = win

        self._upload_wave_texture()
        # Set initial opacity so the first paint doesn't pop in at 1.0.
        try:
            win.set_opacity(OPACITY_BREATH_MIN)
        except Exception:
            pass

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
        if not self._visible or not self._window or not self._wave:
            self._ticker_src = None
            return False
        now = time.monotonic()
        phase = (now - self._t0) / BREATH_PERIOD_S * 2.0 * math.pi
        b01 = 0.5 * (1.0 + math.sin(phase))
        opacity = (OPACITY_BREATH_MIN
                   + (OPACITY_BREATH_MAX - OPACITY_BREATH_MIN) * b01)

        if self._glint_t is not None:
            dt = now - self._glint_t
            if dt < GLINT_HOLD_S:
                opacity = GLINT_PEAK_OPACITY
            elif dt < GLINT_HOLD_S + GLINT_DECAY_S:
                t = (dt - GLINT_HOLD_S) / GLINT_DECAY_S
                opacity = GLINT_PEAK_OPACITY + (opacity - GLINT_PEAK_OPACITY) * t
            else:
                self._glint_t = None

        try:
            self._window.set_opacity(max(0.0, min(1.0, opacity)))
        except Exception:
            pass
        self._upload_wave_texture()
        return True

    def _upload_wave_texture(self) -> None:
        """Render the waveform bars onto a transparent background."""
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
            cr.paint()
            cr.set_operator(_cairo.OPERATOR_OVER)
            n = len(bars)
            if n > 0:
                cx0 = (cw - n * (WAVE_BAR_W + WAVE_BAR_GAP)) / 2
                for i, v in enumerate(bars):
                    bh = max(2.0, v * max_bar_h)
                    x = cx0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
                    y = (ch - bh) / 2
                    cr.set_source_rgba(0.50, 0.95, 0.65, 0.95)
                    _rounded_rect(cr, x, y, WAVE_BAR_W, bh, 1.5)
                    cr.fill()
            tex = _image_surface_to_texture(surf, cw, ch)
            self._wave.set_paintable(tex)
            self._wave_tex = tex
        except Exception:
            pass


def _image_surface_to_texture(surf, cw: int, ch: int) -> Gdk.Texture:
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

"""Top-center floating overlay: 圆角胶囊 + 波形 + 文字.

视觉规范(用户反馈,2026-06-21):
  1. 整体圆滑,圆角
  2. 动效只发生在外边,文字与候选字区域保持绝对静态、
     纯白高对比度
  3. 呼吸: alpha 在 0.78 ~ 0.98 之间循环(2.4s 周期)
  4. 敲击触发流光(每打一字闪一次): 短暂跳到 1.00 持续 220ms,
     再 400ms 衰减回呼吸
  5. 越柔和越好

GTK3 端口实现 (原本是 GTK4):
  - `Gtk.Window(type=POPUP)` + `set_app_paintable(True)` + RGBA
    visual 让背景真正透明。
  - 主面板用一层 `Gtk.DrawingArea`,通过 `draw` 信号画圆角胶囊
    + 波形 bars。文字仍走 `Gtk.Label`,叠在 box 上方靠 stacking。
  - 呼吸/流光仍走 `Gtk.Window.set_opacity()`,GTK3 在 X11 下需要
    合成器(我们这里是 GNOME on X11,有 Mutter 合成器,可以
    工作)。
  - 60ms `GLib.timeout` 驱动重绘 (`queue_draw`)。

The window is borderless (POPUP) and `set_accept_focus(False)`,
so the floating 波形胶囊 never steals focus from the user's
input field.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Optional

import gi

import cairo

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gdk, Gtk  # type: ignore

logger = logging.getLogger(__name__)

# ----- Geometry -----
OVERLAY_WIDTH = 560
OVERLAY_HEIGHT = 116
WAVE_BARS = 24
WAVE_BAR_W = 4
WAVE_BAR_GAP = 3
WAVE_BAR_MAX_H = 24
CORNER_PX = 18  # 圆角半径(px)

# ----- Animation parameters -----
TICK_MS = 60                              # ~16 fps
BREATH_PERIOD_S = 2.4                     # 一次呼吸周期
OPACITY_BREATH_MIN = 0.78
OPACITY_BREATH_MAX = 0.98
GLINT_PEAK_OPACITY = 1.00
GLINT_HOLD_S = 0.22
GLINT_DECAY_S = 0.40

# Cairo colors -- duplicated of the GTK4 CSS values for parity.
PANEL_BG_RGBA = (18 / 255.0, 20 / 255.0, 26 / 255.0, 0.78)
PANEL_BORDER_RGBA = (1.0, 1.0, 1.0, 0.30)
WAVE_BAR_RGBA = (0.50, 0.95, 0.65, 0.95)


class Overlay:
    """The floating 圆角 波形胶囊 (GTK3)."""

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._panel: Optional[Gtk.DrawingArea] = None
        self._label: Optional[Gtk.Label] = None
        self._bars: deque = deque([0.0] * WAVE_BARS, maxlen=WAVE_BARS)
        self._ticker_src: Optional[int] = None
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
            self._window.show_all()
            self._reposition()
            self._visible = True
        self._arm_ticker()

    def hide(self) -> None:
        if self._window and self._visible:
            self._window.hide()
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
        # POPUP 窗口: 无装饰, 不抢焦点, 不入任务栏.
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_title("doubao-input overlay")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        win.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)

        # 让窗口真正透明: app_paintable + RGBA visual.
        # 没有这一步, 背景会变成默认主题色(纯白), 圆角外露白底.
        try:
            screen = win.get_screen()
            visual = screen.get_rgba_visual()
            if visual is not None:
                win.set_visual(visual)
        except Exception:
            pass
        win.set_app_paintable(True)

        # 唯一子 widget: 一个 Overlay 容器, 底层是画圆角面板的
        # DrawingArea, 上层叠一个 Label 显示文字 (静态, 不参与
        # 呼吸动画的颜色变化).
        ov = Gtk.Overlay()

        panel = Gtk.DrawingArea()
        panel.set_size_request(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        panel.connect("draw", self._on_draw_panel)
        ov.add(panel)
        self._panel = panel

        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_yalign(0.5)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_line_wrap(True)
        # GTK3: PangoWrapMode.WORD = 0
        label.set_line_wrap_mode(0)
        label.set_size_request(360, -1)
        label.set_width_chars(14)
        label.set_lines(2)
        label.set_halign(Gtk.Align.CENTER)
        # The label sits in the lower half (under the waveform area).
        # Top padding pushes it past the wave row; CSS not necessary.
        label.set_margin_top(48)
        label.set_margin_bottom(12)
        label.set_margin_start(18)
        label.set_margin_end(18)
        self._label = label
        ov.add_overlay(label)

        win.add(ov)
        self._window = win

        try:
            win.set_opacity(OPACITY_BREATH_MIN)
        except Exception:
            pass

    def _reposition(self) -> None:
        """Center horizontally near the top of the primary monitor."""
        if self._window is None:
            return
        try:
            screen = self._window.get_screen()
            display = screen.get_display() if screen else None
            geo = None
            if display is not None and hasattr(display, "get_monitor"):
                monitor = display.get_primary_monitor() if hasattr(
                    display, "get_primary_monitor"
                ) else None
                if monitor is None and hasattr(display, "get_monitor"):
                    monitor = display.get_monitor(0)
                if monitor is not None:
                    geo = monitor.get_geometry()
            if geo is None and screen is not None:
                # Fallback for older GTK3: use screen dimensions
                geo_w = screen.get_width()
                geo_h = screen.get_height()
                x = (geo_w - OVERLAY_WIDTH) // 2
                y = 24
            else:
                x = geo.x + (geo.width - OVERLAY_WIDTH) // 2
                y = geo.y + 24
            self._window.move(x, y)
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
        if not self._visible or not self._window:
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
        # 重画波形/面板
        if self._panel is not None:
            self._panel.queue_draw()
        return True

    # ---- cairo drawing ----

    def _on_draw_panel(self, widget, cr) -> bool:
        """Draw the rounded translucent panel + waveform bars."""
        try:
            alloc = widget.get_allocation()
            w = alloc.width
            h = alloc.height

            # Clear the window's own transparent background first
            # (otherwise GTK fills it with theme color before draw).
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()

            # Rounded panel background.
            _rounded_path(cr, 0, 0, w, h, CORNER_PX)
            cr.set_source_rgba(*PANEL_BG_RGBA)
            cr.fill_preserve()
            cr.set_source_rgba(*PANEL_BORDER_RGBA)
            cr.set_line_width(1.5)
            cr.stroke()

            # Waveform bars (centered horizontally, upper area).
            bars = list(self._bars)
            has_text = bool(self._text)
            max_bar_h = WAVE_BAR_MAX_H if not has_text else 18
            n = len(bars)
            wave_total_w = n * (WAVE_BAR_W + WAVE_BAR_GAP)
            wave_x0 = (w - wave_total_w) / 2
            wave_cy = 30  # 中线 y, 留出顶部 padding 给胶囊
            cr.set_source_rgba(*WAVE_BAR_RGBA)
            for i, v in enumerate(bars):
                bh = max(2.0, v * max_bar_h)
                x = wave_x0 + i * (WAVE_BAR_W + WAVE_BAR_GAP)
                y = wave_cy - bh / 2
                _rounded_path(cr, x, y, WAVE_BAR_W, bh, 1.5)
                cr.fill()
        except Exception as e:
            logger.debug("overlay draw failed: %s", e)
        return False


def _rounded_path(cr, x, y, w, h, r) -> None:
    """Append a rounded-rectangle subpath to cr."""
    if w <= 0 or h <= 0:
        return
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()

"""Top-center floating overlay: 透明背景 + 文字闪烁.

简化设计(2026-06-22): 之前的圆角胶囊 + 波形 bars + 呼吸
(opacity 0.78~0.98, 2.4s 周期) + 流光(敲击跳到 1.0 后衰减) 由一个
60ms GLib timeout 持续驱动 set_opacity + queue_draw. 在 X11+Mutter
合成下反复触发窗口 recompose, 主线程被拖累, 顺带让 Ctrl+V 注入的
输入也变得巨卡无比.

现在只保留:
  - 透明 POPUP 窗口 (RGBA visual, app_paintable)
  - 居中的 Gtk.Label 显示文字 (CSS 半透明深色背景 + 圆角,
    保证任意桌面背景下都可读)
  - 窗口 opacity 在 0.78 ~ 0.98 之间以 2.4s 周期正弦呼吸,
    作为活动指示

公开 API 保持向后兼容: push_rms / on_keystroke 保留为 no-op,
app.py 无需改动.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore

logger = logging.getLogger(__name__)

# ----- Geometry -----
OVERLAY_WIDTH = 480
OVERLAY_HEIGHT = 56
# Label 自身的深色半透明背景, 保证文字在任意桌面背景下都可读.
# 窗口本身仍然全透明 (RGBA visual + app_paintable), 圆角外不漏色.
LABEL_BG_RGBA = "rgba(18, 20, 26, 0.85)"
LABEL_RADIUS_PX = 12

# ----- Breathing -----
# 用正弦在 OPACITY_BREATH_MIN ~ OPACITY_BREATH_MAX 之间循环, 周期
# BREATH_PERIOD_S. 每 TICK_MS 重算一次相位并 set_opacity.
# 80ms 一个 tick 在 2.4s 周期下得到 30 个采样, sin 曲线肉眼完全平滑.
TICK_MS = 80
BREATH_PERIOD_S = 2.4
OPACITY_BREATH_MIN = 0.78
OPACITY_BREATH_MAX = 0.98


class Overlay:
    """极简浮层: 透明窗口 + 闪烁文字. (GTK3)"""

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._label: Optional[Gtk.Label] = None
        self._breath_src: Optional[int] = None
        self._t0: float = 0.0
        self._text: str = ""
        self._status_text: str = ""
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
        self._arm_breath()

    def hide(self) -> None:
        if self._window and self._visible:
            self._window.hide()
            self._visible = False
        self._cancel_breath()

    def set_text(self, text: str) -> None:
        self._text = text
        if self._window:
            self._refresh_label()
            self._arm_breath()

    def set_status(self, status: str) -> None:
        self._status_text = status
        if self._window:
            self._refresh_label()
            self._arm_breath()

    def push_rms(self, rms: float) -> None:
        # no-op: 不再绘制波形, 不再有任何 per-frame 状态变更
        return

    def on_keystroke(self) -> None:
        # no-op: 不再有流光效果
        return

    # ---- internals ----

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
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
        # 透明背景: app_paintable + RGBA visual
        try:
            screen = win.get_screen()
            visual = screen.get_rgba_visual() if screen else None
            if visual is not None:
                win.set_visual(visual)
        except Exception:
            pass
        win.set_app_paintable(True)

        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_yalign(0.5)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(0)  # PangoWrapMode.WORD
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        self._label = label
        win.add(label)
        self._window = win

        # Label 半透明深色背景 + 圆角: 通过 CSS 直接作用在 label 上,
        # 窗口本身保持全透明, 圆角外仍是桌面背景.
        try:
            css_bytes = (
                "label {"
                f"  background-color: {LABEL_BG_RGBA};"
                f"  border-radius: {LABEL_RADIUS_PX}px;"
                "  padding: 10px 18px;"
                "  color: #ffffff;"
                "}"
            ).encode("utf-8")
            provider = Gtk.CssProvider()
            provider.load_from_data(css_bytes)
            label.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as e:
            logger.debug("overlay css load failed: %s", e)

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
                geo_w = screen.get_width()
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
        display = self._text if self._text else self._status_text
        if len(display) > 600:
            display = display[-600:]
        from html import escape
        self._label.set_markup(
            f'<span foreground="#ffffff" weight="bold">'
            f"{escape(display)}"
            "</span>"
        )

    def _arm_breath(self) -> None:
        if self._breath_src is not None:
            return
        if self._t0 == 0.0:
            self._t0 = time.monotonic()
        self._breath_src = GLib.timeout_add(TICK_MS, self._breath_tick)

    def _cancel_breath(self) -> None:
        if self._breath_src is None:
            return
        try:
            GLib.source_remove(self._breath_src)
        except Exception:
            pass
        self._breath_src = None

    def _breath_tick(self) -> bool:
        if not self._visible or not self._window:
            self._breath_src = None
            return False
        phase = (time.monotonic() - self._t0) / BREATH_PERIOD_S * 2.0 * math.pi
        b01 = 0.5 * (1.0 + math.sin(phase))
        op = OPACITY_BREATH_MIN + (OPACITY_BREATH_MAX - OPACITY_BREATH_MIN) * b01
        try:
            self._window.set_opacity(max(0.0, min(1.0, op)))
        except Exception:
            pass
        return True
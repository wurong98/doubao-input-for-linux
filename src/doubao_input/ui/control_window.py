"""Control / status window.

Single-instance app: re-launching the binary surfaces this window
(via app.do_activate -> present()).

Content:
- 登录状态 (Logged in / Not logged in / Checking)
- 「登录豆包」按钮  -> opens the WebKitGTK login window
- 使用说明
- 退出按钮
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # type: ignore

from doubao_input.doubao.app_state import AppState, LoginStatus

logger = logging.getLogger(__name__)


class ControlWindow:
    def __init__(
        self,
        app_state: AppState,
        on_login_clicked: Callable[[], None],
        on_quit_clicked: Callable[[], None],
        on_check_mic_clicked: Callable[[], None],
        app: Optional[Gtk.Application] = None,
    ) -> None:
        self._app_state = app_state
        self._on_login = on_login_clicked
        self._on_quit = on_quit_clicked
        self._on_check_mic = on_check_mic_clicked
        self._app = app
        self._window: Optional[Gtk.Window] = None
        self._status_label: Optional[Gtk.Label] = None
        self._app_state.connect("login-status-changed", self._on_status_changed)

    # ---- public ----

    def show(self) -> None:
        self._ensure_window()
        self._window.present()
        self._refresh_status()
        # DEBUG: introspect our own toplevel state to prove we're actually mapped
        import logging
        import time
        log = logging.getLogger(__name__)
        try:
            time.sleep(0.4)
            win = self._window
            w = win.get_width()
            h = win.get_height()
            mapped = win.get_mapped() if hasattr(win, "get_mapped") else "?"
            visible = win.get_visible() if hasattr(win, "get_visible") else "?"
            title = win.get_title()
            surface = win.get_surface()
            scale = surface.get_scale_factor() if surface else "?"
            log.warning(
                "WINDOW_STATE w=%d h=%d mapped=%s visible=%s title=%r scale=%s",
                w, h, mapped, visible, title, scale,
            )
            # Try to ask the compositor about the toplevel we own.
            try:
                toplevel = None
                if hasattr(win, "get_toplevel"):
                    toplevel = win.get_toplevel()
                native = win.get_native()
                log.warning("WINDOW_NATIVE class=%s has_toplevel=%s",
                            type(native).__name__ if native else None,
                            toplevel is not None)
            except Exception as e:
                log.warning("WINDOW_NATIVE err: %s", e)
        except Exception as e:
            log.warning("window introspection failed: %s", e)

    def _screenshot_to(self, path: str) -> None:
        """Render this window's surface to a PNG using GTK's paint API.
        Works on Wayland via the GdkSurface snapshot path."""
        import cairo
        win = self._window
        if win is None:
            return
        try:
            w = win.get_width()
            h = win.get_height()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(w, 1), max(h, 1))
            cr = cairo.Context(surface)
            # GTK4: GtkWidget.snapshot() produces a GdkPaintable, not Cairo.
            # Easier: use the snapshot path via the snapshot closure.
            from gi.repository import Gdk, Gtk  # type: ignore
            snapshot = Gtk.Snapshot()
            win.snapshot(snapshot)
            node = snapshot.free_to_node()
            # Render to a Cairo surface
            renderer = win.get_native().get_renderer() if hasattr(win.get_native(), 'get_renderer') else None
            # Fallback: use Gdk.Texture from paintable
            paintable = win.get_paintable() if hasattr(win, 'get_paintable') else None
            if paintable is not None:
                texture = paintable.get_current_image() if hasattr(paintable, 'get_current_image') else None
                if texture is not None:
                    texture.save_to_png(path)
                    return
            surface.write_to_png(path)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("screenshot_to failed: %s", e)

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)

    def destroy(self) -> None:
        if self._window:
            self._window.destroy()
            self._window = None

    # ---- internals ----

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        win = Gtk.ApplicationWindow(application=self._app) if self._app is not None else Gtk.Window()
        win.set_title("豆包语音输入法")
        win.set_default_size(440, 360)
        win.set_resizable(False)
        win.set_destroy_with_parent(True)
        # Aggressive present: get the display time of the most recent user
        # input event so the window manager treats this as a real user
        # activation (Gdk.CURRENT_TIME is 0 and often ignored by
        # compositors). Also center on the primary monitor.
        try:
            from gi.repository import Gdk  # type: ignore
            display = Gdk.Display.get_default()
            seat = display.get_default_seat() if display else None
            time = Gdk.CURRENT_TIME
            if seat is not None:
                # Use the latest user-input timestamp so the WM
                # accepts this as a real "raise to front" request.
                ev = seat.get_last_event() if hasattr(seat, 'get_last_event') else None
                if ev is not None and hasattr(ev, 'get_time'):
                    time = ev.get_time()
            win.present_with_time(time)
        except Exception:
            try:
                win.present()
            except Exception:
                pass
        # Center on primary monitor (best-effort; ignores position on
        # wayland but at least we set the default geometry).
        try:
            from gi.repository import Gdk  # type: ignore
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    mon = monitors.get_item(0)
                    geo = mon.get_geometry()
                    w, h = 440, 360
                    x = geo.x + (geo.width - w) // 2
                    y = geo.y + (geo.height - h) // 2
                    if hasattr(win, "default_size"):
                        # set_default_size then set_position if available
                        try:
                            win.set_default_size(w, h)
                        except Exception:
                            pass
                    _ = (x, y)  # hint for future set_position
        except Exception:
            pass

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(20)
        box.set_margin_bottom(20)

        title = Gtk.Label()
        title.set_xalign(0.0)
        title.set_markup("<b><big>豆包语音输入法</big></b>")
        box.append(title)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_wrap(True)
        self._status_label.set_selectable(True)
        box.append(self._status_label)

        login_btn = Gtk.Button.new_with_label("登录豆包")
        login_btn.connect("clicked", lambda *_: self._on_login())
        box.append(login_btn)

        check_btn = Gtk.Button.new_with_label("检查麦克风 (输出 RMS)")
        check_btn.connect("clicked", lambda *_: self._on_check_mic())
        box.append(check_btn)

        # Diagnostic button: inject a fixed string via the same path the
        # voice pipeline uses. Lets the user verify wl-copy + uinput Ctrl+V
        # end-to-end without having to log in first.
        test_btn = Gtk.Button.new_with_label("测试粘贴 (注入 'hello 测试 123')")
        test_btn.connect("clicked", lambda *_: self._on_test_inject())
        box.append(test_btn)

        help = Gtk.Label()
        help.set_xalign(0.0)
        help.set_yalign(0.0)
        help.set_wrap(True)
        help.set_markup(
            "<small>"
            "<b>使用方式</b>\n"
            "1. 在任意输入框聚焦\n"
            "2. 按住 <b>右 Ctrl</b> 说话\n"
            "3. 松开右 Ctrl,识别结果自动粘贴到当前输入框\n"
            "\n"
            "<b>取消</b>:在录音中按 ESC 或在悬浮胶囊上点取消\n"
            "\n"
            "<b>注意</b>\n"
            "• 终端里需手动 Ctrl+Shift+V\n"
            "• 凭证保存在 ~/.config/doubao-input/asr_params.json\n"
            "</small>"
        )
        box.append(help)

        quit_btn = Gtk.Button.new_with_label("退出")
        quit_btn.connect("clicked", lambda *_: self._on_quit())
        box.append(quit_btn)

        win.set_child(box)
        self._window = win

    def _on_status_changed(self, *_args) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self._status_label:
            return
        s = self._app_state.login_status
        if s == LoginStatus.LOGGED_IN:
            self._status_label.set_markup("状态:<b>已登录</b>  ✅")
        elif s == LoginStatus.NOT_LOGGED_IN:
            self._status_label.set_markup(
                "状态:<b>未登录</b>  ❌\n请点击「登录豆包」完成一次登录。"
            )
        else:
            self._status_label.set_markup("状态:<b>检测中…</b>")

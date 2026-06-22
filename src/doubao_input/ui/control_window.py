"""Control / status window.

Single-instance app: re-launching the binary surfaces this window
(via app activate -> present()).

Content:
- 登录状态 (Logged in / Not logged in / Checking)
- 「登录豆包」按钮  -> opens the WebKitGTK login window
- 使用说明
- 退出按钮

GTK3 port note: 原始版本写给 GTK4，本文件已降到 GTK3。GTK3 不支持
`Window.set_child`（用 `add`）、`Box.append`（用 `pack_start`），也没有
`close-request`（用 `delete-event`），并且 widget 默认不可见（必须
`show_all()`）。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
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
        on_test_inject_clicked: Optional[Callable[[], None]] = None,
        app: Optional[Gtk.Application] = None,
    ) -> None:
        self._app_state = app_state
        self._on_login = on_login_clicked
        self._on_quit = on_quit_clicked
        self._on_check_mic = on_check_mic_clicked
        self._on_test_inject = on_test_inject_clicked
        self._app = app
        self._window: Optional[Gtk.Window] = None
        self._status_label: Optional[Gtk.Label] = None
        self._app_state.connect("login-status-changed", self._on_status_changed)

    # ---- public ----

    def show(self) -> None:
        self._ensure_window()
        # GTK3: child widgets are hidden by default, must show_all() first.
        self._window.show_all()
        self._window.present()
        self._refresh_status()

    def hide(self) -> None:
        if self._window:
            self._window.hide()

    def destroy(self) -> None:
        if self._window:
            self._window.destroy()
            self._window = None

    # ---- internals ----

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        if self._app is not None:
            win = Gtk.ApplicationWindow(application=self._app)
        else:
            win = Gtk.Window()
        win.set_title("豆包语音输入法")
        win.set_default_size(440, 360)
        win.set_resizable(False)
        win.set_position(Gtk.WindowPosition.CENTER)
        # Hide instead of destroy on the close button so the user can
        # re-open via the desktop entry without losing app state.
        win.connect("delete-event", self._on_delete_event)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(20)
        box.set_margin_bottom(20)

        # Header: logo + title side by side
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_halign(Gtk.Align.START)
        logo_img = self._try_load_logo()
        if logo_img is not None:
            header.pack_start(logo_img, False, False, 0)

        title = Gtk.Label()
        title.set_xalign(0.0)
        title.set_valign(Gtk.Align.CENTER)
        title.set_markup("<b><big>豆包语音输入法</big></b>")
        header.pack_start(title, False, False, 0)
        box.pack_start(header, False, False, 0)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        self._status_label.set_selectable(True)
        box.pack_start(self._status_label, False, False, 0)

        login_btn = Gtk.Button.new_with_label("登录豆包")
        login_btn.connect("clicked", lambda *_: self._on_login())
        box.pack_start(login_btn, False, False, 0)

        check_btn = Gtk.Button.new_with_label("检查麦克风 (输出 RMS)")
        check_btn.connect("clicked", lambda *_: self._on_check_mic())
        box.pack_start(check_btn, False, False, 0)

        # Diagnostic button: inject a fixed string via the same path the
        # voice pipeline uses. Lets the user verify clipboard + uinput Ctrl+V
        # end-to-end without having to log in first.
        test_btn = Gtk.Button.new_with_label("测试粘贴 (注入 'hello 测试 123')")
        if self._on_test_inject is not None:
            test_btn.connect("clicked", lambda *_: self._on_test_inject())
        else:
            test_btn.set_sensitive(False)
        box.pack_start(test_btn, False, False, 0)

        help = Gtk.Label()
        help.set_xalign(0.0)
        help.set_yalign(0.0)
        help.set_line_wrap(True)
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
            "• 凭证保存在 ~/.config/doubao-input/doubao_params.json\n"
            "</small>"
        )
        box.pack_start(help, True, True, 0)

        quit_btn = Gtk.Button.new_with_label("退出")
        quit_btn.connect("clicked", lambda *_: self._on_quit())
        box.pack_start(quit_btn, False, False, 0)

        win.add(box)
        self._window = win

    @staticmethod
    def _try_load_logo() -> Optional[Gtk.Image]:
        try:
            img = Gtk.Image.new_from_file(
                "/usr/share/icons/hicolor/128x128/apps/doubao-input.png"
            )
            img.set_pixel_size(64)
            return img
        except Exception:
            pass
        try:
            from pathlib import Path
            here = Path(__file__).resolve().parent
            bundled = here.parent / "resources" / "logo-128.png"
            if bundled.exists():
                img = Gtk.Image.new_from_file(str(bundled))
                img.set_pixel_size(64)
                return img
        except Exception:
            pass
        return None

    def _on_delete_event(self, window, _event) -> bool:
        # True = "we handled it; don't actually destroy the window".
        window.hide()
        return True

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

"""可选顶栏图标：GTK 3 AppIndicator 在独立进程运行，避免与 GTK 4 冲突。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys

logger = logging.getLogger(__name__)


def start_tray():
    """缺少托盘依赖或桌面扩展时，主程序仍然正常运行。"""
    try:
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(os.getpid())],
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("Tray unavailable: %s", exc)
        return None


def main() -> int:
    import gi

    try:
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gio, GLib, Gtk
    except (ImportError, ValueError):
        return 0

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    actions = Gio.DBusActionGroup.get(bus, "com.doubao.Input", "/com/doubao/Input")
    icon = Path(__file__).resolve().parent.parent / "resources" / "logo-128.png"
    indicator = AyatanaAppIndicator3.Indicator.new(
        "doubao-input", str(icon),
        AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_title("豆包语音输入法")
    menu = Gtk.Menu()
    for label, action in (("打开豆包语音输入", "show-control"), ("退出语音输入", "quit")):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda _item, name=action: actions.activate_action(name, None))
        menu.append(item)
    hint = Gtk.MenuItem(label="按住右 Ctrl 说话，松开输入")
    hint.set_sensitive(False)
    menu.prepend(hint)
    menu.show_all()
    indicator.set_menu(menu)
    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

    # 主程序退出或异常终止后，图标进程也自动清理。
    parent_pid = int(sys.argv[1])
    def check_parent():
        if os.getppid() != parent_pid:
            Gtk.main_quit()
            return False
        return True
    GLib.timeout_add_seconds(1, check_parent)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

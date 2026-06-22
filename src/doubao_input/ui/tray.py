"""系统托盘图标 (AppIndicator3, Ubuntu/GNOME on X11).

CLAUDE.md 项目须知 (节选):
  "GNOME 48 has no StatusNotifierItem by default — `ui.tray` (if added)
   must degrade silently, not fail."

因此本模块在以下任一情况都静默退化, 不抛异常:
  - GI 绑定 `gir1.2-appindicator3-0.1` 缺失;
  - 桌面没有 indicator 扩展, 图标不显示.
主程序仍可通过控制窗口 + 桌面图标完整使用.

参考: Ubuntu 20.04 自带 `gir1.2-appindicator3-0.1` + ubuntu-appindicators
GNOME Shell 扩展, 默认可用. 图标传绝对路径避免依赖 hicolor 主题安装.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import gi

logger = logging.getLogger(__name__)

# AppIndicator3 是 GI 绑定, GTK3 进程里直接 import.
_HAS_INDICATOR = False
try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # type: ignore

    _HAS_INDICATOR = True
except (ImportError, ValueError) as _exc:
    logger.warning("AppIndicator3 not available: %s", _exc)

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore


def _find_icon_path() -> Optional[str]:
    """返回托盘图标的绝对路径; 找不到返回 None."""
    # 1. 已安装到 hicolor (deb/AppImage 场景)
    candidate = Path("/usr/share/icons/hicolor/128x128/apps/doubao-input.png")
    if candidate.exists():
        return str(candidate)
    # 2. 开发模式: 包内 resources/
    here = Path(__file__).resolve().parent
    for name in ("logo-128.png", "logo.png"):
        bundled = here.parent / "resources" / name
        if bundled.exists():
            return str(bundled)
    return None


class Tray:
    """AppIndicator-based 托盘.

    Callbacks (都在 GTK 主线程调用):
      on_show_window(): 用户从托盘点 "显示主窗口"
      on_check_mic():   用户点 "检查麦克风"
      on_quit():        用户点 "退出"
    """

    def __init__(
        self,
        on_show_window: Callable[[], None],
        on_check_mic: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_show_window = on_show_window
        self._on_check_mic = on_check_mic
        self._on_quit = on_quit
        self._indicator = None
        self._build()

    @staticmethod
    def is_available() -> bool:
        return _HAS_INDICATOR

    def _build(self) -> None:
        if not _HAS_INDICATOR:
            logger.info("tray disabled: AppIndicator3 missing")
            return

        icon_path = _find_icon_path()
        # AppIndicator3.Indicator.new(id, icon_name_or_path, category).
        # 第二个参数: 如果是绝对路径会被当 file://, 否则当主题图标名.
        # 我们用绝对路径, 避免依赖 hicolor 主题安装.
        try:
            ind = AppIndicator3.Indicator.new(
                "doubao-input",
                icon_path or "audio-input-microphone",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
        except Exception as e:
            logger.warning("Indicator.new failed: %s", e)
            return

        ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        try:
            ind.set_title("豆包语音输入法")
        except Exception:
            pass  # 旧版没有这个方法

        menu = Gtk.Menu()

        # 显示主窗口
        item_show = Gtk.MenuItem.new_with_label("显示主窗口")
        item_show.connect("activate", lambda *_: self._on_show_window())
        menu.append(item_show)

        # 检查麦克风 (复用主程序的 _check_mic)
        item_mic = Gtk.MenuItem.new_with_label("检查麦克风")
        item_mic.connect("activate", lambda *_: self._on_check_mic())
        menu.append(item_mic)

        menu.append(Gtk.SeparatorMenuItem())

        # 退出
        item_quit = Gtk.MenuItem.new_with_label("退出")
        item_quit.connect("activate", lambda *_: self._on_quit())
        menu.append(item_quit)

        menu.show_all()
        ind.set_menu(menu)
        # AppIndicator 左键默认无动作, 我们把 "secondary activate target"
        # 设成 "显示主窗口", 这样支持中键/单击的 indicator 实现也能直接
        # 唤起主窗口 (ubuntu-appindicators 扩展支持这个).
        try:
            ind.set_secondary_activate_target(item_show)
        except Exception:
            pass

        self._indicator = ind
        self._menu = menu  # 持有引用避免被回收
        logger.info("tray: AppIndicator created (icon=%s)", icon_path)

    def set_recording(self, recording: bool) -> None:
        """录音状态时换 ATTENTION 图标 (如果 indicator 支持)."""
        if self._indicator is None:
            return
        try:
            status = (
                AppIndicator3.IndicatorStatus.ATTENTION
                if recording
                else AppIndicator3.IndicatorStatus.ACTIVE
            )
            self._indicator.set_status(status)
        except Exception:
            pass

    def destroy(self) -> None:
        # AppIndicator 没有显式 destroy, 切到 PASSIVE 让它隐藏.
        if self._indicator is not None:
            try:
                self._indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass
            self._indicator = None

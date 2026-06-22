"""Text injection: copy-to-clipboard + simulate Ctrl+V via uinput,
或对终端 / VSCode-类窗口直接 `xdotool type` 逐字符输入.

The only reliable way to "paste" into a native Wayland app on GNOME
(Mutter does not implement the virtual-keyboard protocol that wtype
needs) is to write text to the clipboard and then synthesize a
Ctrl+V keypress via a /dev/uinput virtual keyboard.

CJK text cannot be typed key-by-key through a virtual keyboard, so
the clipboard path is mandatory for Chinese 在普通应用里. 但是有两类
窗口不能用 Ctrl+V:

  1. 终端 (gnome-terminal/alacritty/kitty/...): Ctrl+V 不是粘贴快捷键,
     需要 Ctrl+Shift+V.
  2. 终端里运行的 TUI (claude-code, less, ...) 会拦截 Ctrl+Shift+V 的
     bracketed-paste 事件, 字符可能进不去或被命令解释器吃掉.

参考 /home/liudf/tools/zhipu-asr/asr_engine.py 的做法: 对这两类窗口走
`xdotool type --clearmodifiers --` 直接合成 KeyPress/KeyRelease 事件
逐字符输入, X11 下 xdotool 通过临时绑定 keysym 支持 Unicode/CJK.

The uinput device is opened lazily on first inject() and kept alive
for the process lifetime to avoid the per-injection cost of creating
and destroying a kernel device.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

from doubao_input.doubao.host_tools import command_candidates

logger = logging.getLogger(__name__)

# 剪贴板写入到读取的最小可靠间隔: clipboard manager (klipper, gpaste, gnome-shell
# 内置) 通常在写入后 20-30ms 内能稳定提供. 原值 80ms 是 Wayland 早期保守值, X11
# 下 xclip 已经写完才返回, 可以更短.
PASTE_DELAY = 0.02  # seconds

# Linux keycodes (from linux/input-event-codes.h)
KEY_LEFTCTRL = 29
KEY_V = 47

# 终端窗口指示词 (xprop WM_CLASS 或 xdotool getwindowname 命中即视作终端).
# 参考 zhipu-asr 的列表; 用前缀/子串匹配以覆盖 gnome-terminal-server 这类后缀.
_TERMINAL_INDICATORS = (
    "terminal", "konsole", "xterm", "gnome-terminal",
    "alacritty", "tilix", "terminator", "kitty", "putty",
    "rxvt", "urxvt", "xfce4-terminal", "mate-terminal",
)
# 编辑器内嵌终端 (如 VSCode terminal panel) 的判定: WM_CLASS 含 code
# 而窗口标题里含下列关键词.
_TERMINAL_IN_TITLE = (
    "terminal", "bash", "zsh", "powershell", "cmd", "python", "fish",
)


def _is_terminal_window(window_name: str, window_class: str) -> bool:
    name = (window_name or "").lower()
    cls = (window_class or "").lower()
    for ind in _TERMINAL_INDICATORS:
        if ind in cls or ind in name:
            return True
        if name.startswith(ind) or cls.startswith(ind):
            return True
    # VSCode 内嵌终端
    if "code" in cls or "code" in name:
        for term in _TERMINAL_IN_TITLE:
            if term in name:
                return True
    return False


class Injector:
    """Inject text into the currently-focused input field."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ui = None  # evdev.UInput
        self._has_xdotool = bool(shutil.which("xdotool"))
        self._has_xprop = bool(shutil.which("xprop"))

    # ---- public ----

    def inject(
        self,
        text: str,
        use_shift: bool = False,
        target_window: Optional[int] = None,
    ) -> bool:
        """把 text 送进 target_window (或当前焦点窗口).

        target_window 应该是调用方在 PTT 按下瞬间用 xdotool getactivewindow
        抓到的窗口 ID. 给了它我们才能可靠地按窗口类型选粘贴方式 (剪贴板
        Ctrl+V vs xdotool type 逐字符). 不给就退化成 "无脑剪贴板+Ctrl+V".
        """
        if not text:
            return False
        with self._lock:
            # 优先走窗口感知路径: zhipu-asr-style.
            if target_window is not None and self._has_xdotool:
                strategy = self._classify_window(target_window)
                logger.info(
                    "inject: window=%s strategy=%s",
                    target_window, strategy,
                )
                if strategy == "type":
                    if self._inject_via_xdotool_type(text, target_window):
                        return True
                    logger.warning("xdotool type failed; falling back to clipboard")
                elif strategy == "paste_shift":
                    return self._inject_via_clipboard(
                        text, use_shift=True, target_window=target_window,
                    )
                elif strategy == "paste":
                    return self._inject_via_clipboard(
                        text, use_shift=False, target_window=target_window,
                    )
            # 兜底: 老的剪贴板+uinput 路径 (Wayland / 无 xdotool / 无 window id).
            return self._inject_via_clipboard(
                text, use_shift=use_shift, target_window=target_window,
            )

    def inject_via_uinput_only(self, use_shift: bool = False) -> bool:
        """Just synthesize Ctrl+V (use when caller already filled clipboard)."""
        with self._lock:
            return self._simulate_paste(use_shift=use_shift)

    def close(self) -> None:
        with self._lock:
            if self._ui is not None:
                try:
                    self._ui.close()
                except Exception:
                    pass
                self._ui = None

    # ---- window classification ----

    def _classify_window(self, window_id: int) -> str:
        """返回 'type' | 'paste_shift' | 'paste'.

        - 'type': WM_CLASS 含 'code' 或属于终端类 -> 用 xdotool type
          逐字符 (避开 Ctrl+V / Ctrl+Shift+V 被拦截).
        - 'paste_shift': 不该到这分支了 (终端走 type), 保留以备 type 失败.
        - 'paste': 普通应用, 剪贴板 + Ctrl+V.
        """
        name, cls = self._get_window_info(window_id)
        is_term = _is_terminal_window(name, cls)
        cls_lower = (cls or "").lower()
        logger.debug(
            "_classify_window: id=%s name=%r class=%r is_term=%s",
            window_id, name, cls, is_term,
        )
        if is_term or "code" in cls_lower:
            return "type"
        return "paste"

    def _get_window_info(self, window_id: int):
        name = ""
        cls = ""
        try:
            r = subprocess.run(
                ["xdotool", "getwindowname", str(window_id)],
                capture_output=True, text=True, timeout=1.5,
            )
            if r.returncode == 0:
                name = (r.stdout or "").strip()
        except Exception as e:
            logger.debug("xdotool getwindowname failed: %s", e)
        if self._has_xprop:
            try:
                r = subprocess.run(
                    ["xprop", "-id", str(window_id), "WM_CLASS"],
                    capture_output=True, text=True, timeout=1.5,
                )
                if r.returncode == 0:
                    # 输出形如:  WM_CLASS(STRING) = "instance", "Class"
                    raw = (r.stdout or "").strip()
                    matches = re.findall(r'"([^"]+)"', raw)
                    if matches:
                        # 取 Class (第二个), instance (第一个) 兜底
                        cls = matches[-1]
            except Exception as e:
                logger.debug("xprop WM_CLASS failed: %s", e)
        return name, cls

    # ---- injection backends ----

    def _inject_via_xdotool_type(self, text: str, window_id: int) -> bool:
        """对终端/VSCode 走 xdotool type, 直接合成逐字符按键事件.

        关键步骤参考 zhipu-asr/asr_engine.py:
          1. windowfocus 把焦点切回目标窗口 (我们的 overlay 已经被 hide,
             但 GNOME 偶尔还残留焦点, 显式 focus 一下最稳).
          2. sleep 0.15 等焦点稳定.
          3. keyup ctrl shift alt 释放残留修饰符 (PTT 用户刚松开右 Ctrl,
             X server 的修饰符状态可能还没回零).
          4. xdotool type --window <id> --clearmodifiers --delay 0 --
             text  逐字符发送 KeyPress/Release.
        """
        try:
            subprocess.run(
                ["xdotool", "windowfocus", str(window_id)],
                check=False, timeout=1.5,
            )
            time.sleep(0.15)
            subprocess.run(
                ["xdotool", "keyup", "ctrl", "shift", "alt"],
                check=False, timeout=1.5,
            )
            # --clearmodifiers: xdotool 自动 keyup 当前按下的修饰符;
            # --delay 1: 每个字符之间间隔 1ms (默认 12ms 对长文本太慢);
            # `--` 结束选项解析, 防止 text 以 - 开头被当成 flag.
            cmd = [
                "xdotool", "type", "--window", str(window_id),
                "--clearmodifiers", "--delay", "1", "--", text,
            ]
            r = subprocess.run(cmd, check=False, timeout=15)
            ok = r.returncode == 0
            logger.info("xdotool type ok=%s (len=%d)", ok, len(text))
            return ok
        except Exception as e:
            logger.warning("xdotool type exception: %s", e)
            return False

    def _inject_via_clipboard(
        self,
        text: str,
        use_shift: bool = False,
        target_window: Optional[int] = None,
    ) -> bool:
        """老路径: 剪贴板 + uinput Ctrl+V (或 Ctrl+Shift+V)."""
        ok_copy = self._copy_to_clipboard(text)
        if not ok_copy:
            logger.error("clipboard copy failed; cannot inject")
            return False
        # 让 clipboard manager 有时间生效.
        time.sleep(PASTE_DELAY)
        # 把焦点切回目标窗口, 否则 uinput 注的 Ctrl+V 可能落在错的地方.
        # 用 stderr 重定向吞掉 `X Error of failed request: BadMatch
        # X_SetInputFocus` — 这条错是当目标窗口未 viewable / iconified 时
        # XSetInputFocus 抛的, xdotool 不退出但日志很吵, 也无碍后续粘贴
        # (失败时再退化到不切焦点直接发 Ctrl+V).
        if target_window is not None and self._has_xdotool:
            try:
                subprocess.run(
                    ["xdotool", "windowfocus", "--sync", str(target_window)],
                    check=False, timeout=1.5,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.03)
                subprocess.run(
                    ["xdotool", "keyup", "ctrl", "shift", "alt"],
                    check=False, timeout=1.5,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.debug("windowfocus failed (non-fatal): %s", e)
        return self._simulate_paste(use_shift=use_shift)

    # ---- internals ----

    def _copy_to_clipboard(self, text: str) -> bool:
        data = text.encode("utf-8")
        # wl-copy first
        for cmd in command_candidates("wl-copy"):
            try:
                subprocess.run(cmd, input=data, check=True, timeout=3)
                logger.info("clipboard: wl-copy ok")
                return True
            except Exception as e:
                logger.debug("wl-copy failed: %s", e)
        # xclip fallback (XWayland only)
        for cmd in command_candidates("xclip"):
            try:
                subprocess.run(
                    cmd + ["-selection", "clipboard"],
                    input=data, check=True, timeout=3,
                )
                logger.info("clipboard: xclip ok")
                return True
            except Exception as e:
                logger.debug("xclip failed: %s", e)
        return False

    def _get_uinput(self):
        if self._ui is not None:
            return self._ui
        import evdev  # type: ignore

        self._ui = evdev.UInput(
            events={evdev.ecodes.EV_KEY: [KEY_LEFTCTRL, KEY_V]},
            name="doubao-input-virtual-kbd",
        )
        logger.info("uinput virtual keyboard created")
        return self._ui

    def _simulate_paste(self, use_shift: bool = False) -> bool:
        """Send a Ctrl+V (or Ctrl+Shift+V) keypress through uinput."""
        try:
            ui = self._get_uinput()
            import evdev  # type: ignore
            import time as _t
            KEY_LEFTSHIFT = 42
            # Some receivers (notably GTK apps and terminals) need
            # measurable time between key events; otherwise the
            # press-release sequence gets coalesced into nothing and
            # the paste shortcut never registers.
            GAP = 0.012  # seconds between events

            def emit(code: int, value: int) -> None:
                ui.write(evdev.ecodes.EV_KEY, code, value)
                ui.syn()
                _t.sleep(GAP)

            # Press
            emit(KEY_LEFTCTRL, 1)
            if use_shift:
                emit(KEY_LEFTSHIFT, 1)
            emit(KEY_V, 1)
            _t.sleep(GAP)
            # Release
            emit(KEY_V, 0)
            if use_shift:
                emit(KEY_LEFTSHIFT, 0)
            emit(KEY_LEFTCTRL, 0)
            logger.info("paste: uinput ok (shift=%s, gap=%.0fms)",
                        use_shift, GAP * 1000)
            return True
        except Exception as e:
            logger.error("uinput paste failed: %s", e)
            return False


"""Text injection: copy-to-clipboard + simulate Ctrl+V via uinput.

The only reliable way to "paste" into a native Wayland app on GNOME
(Mutter does not implement the virtual-keyboard protocol that wtype
needs) is to write text to the clipboard and then synthesize a
Ctrl+V keypress via a /dev/uinput virtual keyboard.

CJK text cannot be typed key-by-key through a virtual keyboard, so
the clipboard path is mandatory for Chinese.

The uinput device is opened lazily on first inject() and kept alive
for the process lifetime to avoid the per-injection cost of creating
and destroying a kernel device.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

from doubao_input.doubao.host_tools import command_candidates

logger = logging.getLogger(__name__)

# Pause after wl-copy so the clipboard manager has settled.
# Also pause after the right-Ctrl physical release to avoid mixing it
# with our injected Left Ctrl.
PASTE_DELAY = 0.08  # seconds

# Linux keycodes (from linux/input-event-codes.h)
KEY_LEFTCTRL = 29
KEY_V = 47


class Injector:
    """Inject text into the currently-focused input field."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ui = None  # evdev.UInput

    # ---- public ----

    def inject(self, text: str, use_shift: bool = False) -> bool:
        """Copy text to clipboard then synthesize Ctrl+V (or Ctrl+Shift+V)."""
        if not text:
            return False
        with self._lock:
            ok_copy = self._copy_to_clipboard(text)
            if not ok_copy:
                logger.error("clipboard copy failed; cannot inject")
                return False
            time.sleep(PASTE_DELAY)
            ok_paste = self._simulate_paste(use_shift=use_shift)
            return ok_paste

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

"""Main GtkApplication: orchestrates trigger, ASR, overlay, injector, login.

Lifecycle:
  1. On first activate: build all components. Show login window if no
     saved credentials; otherwise just start the PTT listener and
     show the control window.
  2. On subsequent activate (single-instance re-launch via the desktop
     entry or `python -m doubao_input` again): just present the control
     window.
  3. Quit: stop everything cleanly.
"""
from __future__ import annotations

import logging
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # type: ignore

from doubao_input.doubao.app_state import AppState, LoginStatus, RecordingState
from doubao_input.doubao.asr_client import ASRClient
from doubao_input.doubao.audio_capture import AudioCapture
from doubao_input.doubao.config import PASTE_DELAY
from doubao_input.doubao.params_store import ParamsStore
from doubao_input.doubao.transcription import TranscriptionManager
from doubao_input.inject.injector import Injector
from doubao_input.trigger.evdev_ptt import EvdevPtt
from doubao_input.ui.control_window import ControlWindow
from doubao_input.ui.overlay import Overlay

logger = logging.getLogger(__name__)


class DoubaoInputApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="com.doubao.Input",
            flags=Gio_APPLICATION_NON_UNIQUE(),
        )
        # PyGObject does NOT dispatch do_activate() automatically in all
        # versions — connect the signal explicitly so the first run
        # actually shows the UI.
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)
        self._setup_done = False
        self.app_state = AppState()
        self._overlay: Overlay | None = None
        self._control: ControlWindow | None = None
        self._login_window = None
        self._ptt: EvdevPtt | None = None
        self._tm: TranscriptionManager | None = None
        self._injector: Injector | None = None
        self._last_press_at: float = 0.0

    # ---- Gtk.Application hooks (signal handlers, not do_*) ----

    def _on_activate(self, _app) -> None:
        logger.info("on_activate: setup_done=%s", self._setup_done)
        if not self._setup_done:
            self._setup_done = True
            self._build()
        if self._control:
            self._control.show()
        # Boot-time proof-of-life: briefly flash the floating overlay so
        # the user can see *something* from this app even before they log
        # in. This is the "弹出输入法" signal. Hide it 3 seconds later.
        from gi.repository import GLib
        GLib.timeout_add(400, self._boot_flash_overlay)

    def _boot_flash_overlay(self) -> bool:
        if self._overlay is None:
            return False
        self._overlay.show("豆包输入法已启动 · 请登录或按住右 Ctrl 说话")
        GLib.timeout_add_seconds(3, lambda: (self._overlay.hide(), False)[1])
        return False

    def _on_shutdown(self, _app) -> None:
        logger.info("shutting down")
        if self._ptt:
            self._ptt.stop()
        if self._injector:
            self._injector.close()

    # ---- build ----

    def _build(self) -> None:
        self._overlay = Overlay()
        self._injector = Injector()

        # ---- TranscriptionManager (state machine) ----
        tm = TranscriptionManager(self.app_state)
        # Replace its audio_capture with our instance so we can wire RMS
        from doubao_input.doubao.audio_capture import AudioCapture
        self._audio_capture = AudioCapture()
        tm.audio_capture = self._audio_capture

        # Wrap start() so that on every recording, RMS is piped to the
        # overlay (waveform). Wrapping is safer than monkey-patching the
        # class globally.
        _ac_orig_start = self._audio_capture.start
        def _start_with_rms(on_audio_data):
            _ac_orig_start(on_audio_data)
            self._audio_capture._on_rms = self._overlay.push_rms
        self._audio_capture.start = _start_with_rms  # type: ignore[assignment]
        _ac_orig_stop = self._audio_capture.stop
        def _stop_clear_rms():
            _ac_orig_stop()
            self._audio_capture._on_rms = None
        self._audio_capture.stop = _stop_clear_rms  # type: ignore[assignment]

        tm.on_show_login = self._show_login
        tm.on_overlay_show = lambda: self._overlay.show("正在启动语音识别…")
        tm.on_overlay_hide = lambda: self._overlay.hide()
        tm.on_overlay_update = lambda text: self._overlay.set_text(text)
        tm.on_paste = self._do_paste
        tm.on_cancel_enabled_changed = lambda enabled: None
        tm.on_auth_expired = self._on_auth_expired
        tm.on_params_needed = self._provide_params
        self._tm = tm

        # ---- Control window ----
        self._control = ControlWindow(
            app_state=self.app_state,
            on_login_clicked=self._show_login,
            on_quit_clicked=self._quit,
            on_check_mic_clicked=self._check_mic,
            app=self,
        )

        # ---- Initial state: cached params? ----
        if ParamsStore.has_saved():
            self.app_state.login_status = LoginStatus.LOGGED_IN
        else:
            self.app_state.login_status = LoginStatus.NOT_LOGGED_IN

        # ---- PTT trigger ----
        self._ptt = EvdevPtt(
            on_press=self._on_ptt_press,
            on_release=self._on_ptt_release,
            on_error=self._on_ptt_error,
        )
        if not self._ptt.start():
            logger.warning("evdev PTT failed to start; will retry in background")
            # Don't block; user can still open the control window to diagnose.

        logger.info("build complete")

    # ---- PTT callbacks (run on GTK main thread) ----

    def _on_ptt_press(self) -> None:
        if not self._tm:
            return
        if self.app_state.login_status != LoginStatus.LOGGED_IN:
            self._overlay.set_text("请先在控制窗口完成登录")
            self._overlay.show("未登录")
            return
        self._tm.handle_press()

    def _on_ptt_release(self) -> None:
        if not self._tm:
            return
        if self.app_state.login_status != LoginStatus.LOGGED_IN:
            self._overlay.hide()
            return
        self._tm.handle_release()

    def _on_ptt_error(self, msg: str) -> None:
        logger.warning("PTT error: %s", msg)

    # ---- Paste ----

    def _do_paste(self, text: str) -> None:
        # Give the physical right-Ctrl key a moment to fully release
        # before we synthesize Left Ctrl, to avoid any chance of them
        # being merged in the receiver.
        time.sleep(PASTE_DELAY)
        if not self._injector:
            return
        self._injector.inject(text, use_shift=False)
        # Clear overlay text shortly after
        GLib.timeout_add(400, lambda: (self._overlay.set_text(""), False)[1])

    # ---- Login ----

    def _show_login(self) -> None:
        from doubao_input.doubao.login_window import LoginWindow
        if self._login_window is None:
            lw = LoginWindow(self.app_state)
            lw._on_login_status_change = self._on_login_detected
            self._login_window = lw
        self._login_window.show()

    def _on_login_detected(self, status: str, nickname: str | None) -> None:
        if status == "loggedIn":
            self.app_state.login_status = LoginStatus.LOGGED_IN
            logger.info("logged in as %s", nickname)
            # Extract params after a short delay, then close the WebView.
            GLib.timeout_add(800, self._extract_and_close_login)

    def _extract_and_close_login(self) -> bool:
        if not self._login_window:
            return False
        def on_params(params):
            if params:
                ParamsStore.save(params)
            if self._login_window:
                try:
                    self._login_window.hide()
                except Exception:
                    pass
                try:
                    self._login_window.destroy()
                except Exception:
                    pass
                self._login_window = None
        self._login_window.extract_params_async(on_params)
        return False

    def _provide_params(self, callback) -> None:
        if self._login_window and self._login_window.is_active:
            self._login_window.extract_params_async(callback)
        else:
            self._show_login()
            callback(None)

    def _on_auth_expired(self) -> None:
        ParamsStore.clear()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        self._show_login()

    def _check_mic(self) -> None:
        """3-second mic test: open stream, push RMS to the overlay, then close."""
        self._overlay.show("麦克风测试中…")
        self._overlay.set_text("请对麦克风说话")

        captured: list[float] = []
        cap = self._audio_capture

        def on_rms_test(rms: float):
            captured.append(rms)
            self._overlay.push_rms(rms)

        # Hook RMS for the test (we restore it after stop)
        cap._on_rms = on_rms_test
        try:
            cap.start(on_audio_data=lambda b: None)
        except Exception as e:
            self._overlay.set_text(f"麦克风启动失败: {e}")
            GLib.timeout_add(1500, lambda: self._overlay.hide() or False)
            return

        def stop_and_report():
            cap.stop()
            cap._on_rms = None  # restore
            if captured:
                peak = max(captured)
                avg = sum(captured) / len(captured)
                self._overlay.set_text(f"测试完成  peak={peak:.2f}  avg={avg:.3f}")
            else:
                self._overlay.set_text("测试完成(无数据)")
            return False

        GLib.timeout_add(3000, stop_and_report)
        GLib.timeout_add(4500, lambda: self._overlay.hide() or False)

    def _quit(self) -> None:
        if self._tm and self.app_state.recording_state != RecordingState.IDLE:
            self._tm.handle_cancel()
        if self._ptt:
            self._ptt.stop()
        if self._injector:
            self._injector.close()
        self.quit()


def Gio_APPLICATION_NON_UNIQUE():
    from gi.repository import Gio  # type: ignore
    return Gio.ApplicationFlags.FLAGS_NONE

"""State machine orchestrator for the recording lifecycle.

Mirrors TranscriptionManager.swift.

Key design decisions:
- GLib.idle_add() marshals callbacks from the asyncio thread to GTK main thread
- GLib.timeout_add() replaces DispatchQueue.main.asyncAfter for delayed execution
- State machine exactly mirrors macOS: idle -> starting -> recording -> stopping -> idle
"""

from __future__ import annotations

import logging

from gi.repository import GLib

from doubao_input.doubao.app_state import AppState, LoginStatus, RecordingState
from doubao_input.doubao.asr_client import ASRClient
from doubao_input.doubao.audio_capture import AudioCapture
from doubao_input.doubao.config import AUTH_EXPIRY_DELAY, STOP_SAFETY_TIMEOUT
from doubao_input.doubao.params_store import ASRParams, ParamsStore

# Minimum press duration to be treated as a real PTT (vs accidental tap).
MIN_PRESS_DURATION = 0.15  # seconds

logger = logging.getLogger(__name__)


class TranscriptionManager:
    """Orchestrates the recording lifecycle."""

    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state
        self.asr_client = ASRClient()
        self.audio_capture = AudioCapture()

        self.using_cached_params = False
        self.awaiting_final_result = False
        self.safety_timer_id: int | None = None
        self._press_started_at: float = 0.0

        # Callbacks set by app.py
        self.on_auth_expired = None  # () -> None
        self.on_show_login = None  # () -> None
        self.on_params_needed = None  # (callback: (ASRParams|None)->None) -> None
        self.on_overlay_show = None  # () -> None
        self.on_overlay_hide = None  # () -> None
        self.on_overlay_update = None  # (text: str) -> None
        self.on_paste = None  # (text: str) -> None
        self.on_cancel_enabled_changed = None  # (enabled: bool) -> None

        self._wire_asr_callbacks()

    def _wire_asr_callbacks(self) -> None:
        """Wire ASR client callbacks to marshal from asyncio to GTK thread."""
        self.asr_client.on_open = lambda: GLib.idle_add(self._on_asr_open)
        self.asr_client.on_result = lambda text: GLib.idle_add(
            self._on_asr_result, text
        )
        self.asr_client.on_finish = lambda: GLib.idle_add(self._on_asr_finish)
        self.asr_client.on_error = lambda err: GLib.idle_add(
            self._on_asr_error, err
        )
        self.asr_client.on_auth_error = lambda: GLib.idle_add(
            self._on_auth_error
        )

    # --- Toggle ---

    def handle_toggle(self) -> None:
        """Called on GTK main thread from hotkey manager.
        Kept for compatibility with the original toggle-style API."""
        state = self.app_state.recording_state
        if state == RecordingState.IDLE:
            self._start_recording()
        elif state in (RecordingState.STARTING, RecordingState.RECORDING):
            self._stop_recording()
        # STOPPING: ignore

    # --- Push-to-hold (primary API for this project) ---

    def handle_press(self) -> None:
        """Right-Ctrl down: start recording (if not already recording)."""
        import time as _t
        self._press_started_at = _t.monotonic()
        if self.app_state.recording_state == RecordingState.IDLE:
            self._start_recording()

    def handle_release(self) -> None:
        """Right-Ctrl up: stop recording and inject (unless a too-brief tap)."""
        import time as _t
        dur = _t.monotonic() - self._press_started_at if self._press_started_at else 0
        self._press_started_at = 0.0
        state = self.app_state.recording_state
        if state not in (RecordingState.STARTING, RecordingState.RECORDING):
            return
        if dur < MIN_PRESS_DURATION:
            logger.info("Press too short (%.3fs), treating as accidental tap", dur)
            self.handle_cancel()
            return
        self._stop_recording()

    def _start_recording(self) -> None:
        if self.app_state.login_status != LoginStatus.LOGGED_IN:
            logger.warning("Not logged in, showing login window")
            if self.on_show_login:
                self.on_show_login()
            return

        logger.info("Starting recording...")
        self._set_state(RecordingState.STARTING)
        self.app_state.transcription_text = ""
        self.app_state.error_message = None
        if self.on_overlay_show:
            self.on_overlay_show()

        # 1. 先发起 WS 连接 (后台 asyncio 线程, 立即返回). ASR client 自带
        #    `_pending_audio` 缓冲, 连接好之前 audio_capture 送来的字节会
        #    缓存, 连上后立刻 flush. 所以这两步可以并行, 而不是串行 ——
        #    实测在 X11/PulseAudio 上 audio_capture.start() 冷态要 ~270ms,
        #    WS 连接也要 ~170ms; 串行 ~440ms, 并行只剩 max(270,170)=270ms.
        cached = ParamsStore.load()
        if cached:
            logger.info("Using cached ASR params")
            self.using_cached_params = True
            self.asr_client.connect(cached)
        elif self.on_params_needed:
            self.using_cached_params = False
            self.on_params_needed(self._on_params_extracted)
        else:
            self.app_state.error_message = "无法获取连接参数，请重新登录"
            GLib.timeout_add(
                int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle
            )
            return

        # 2. 然后开 PortAudio (这是真正阻塞的那一步, ~270ms 冷态 / ~5ms 热态).
        try:
            self.audio_capture.start(on_audio_data=self.asr_client.send_audio)
        except Exception as e:
            logger.error("Audio capture failed: %s", e)
            self.app_state.error_message = "麦克风启动失败"
            # 音频起不来, 把刚连上的 WS 也拆掉
            try:
                self.asr_client.disconnect()
            except Exception:
                pass
            GLib.timeout_add(
                int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle
            )
            return

    def _stop_recording(self) -> None:
        logger.info("Stopping recording...")
        self._set_state(RecordingState.STOPPING)
        self.audio_capture.stop()
        self.asr_client.finish_sending()
        self.awaiting_final_result = True

        # Safety timeout
        self.safety_timer_id = GLib.timeout_add(
            int(STOP_SAFETY_TIMEOUT * 1000), self._safety_timeout
        )

    def _safety_timeout(self) -> bool:
        if self.app_state.recording_state == RecordingState.STOPPING:
            logger.info("Safety timeout, completing with current text")
            self.awaiting_final_result = False
            self._complete_transcription()
        self.safety_timer_id = None
        return GLib.SOURCE_REMOVE

    # --- ASR callbacks (on GTK main thread via GLib.idle_add) ---

    def _on_asr_open(self) -> bool:
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)
        return GLib.SOURCE_REMOVE

    def _on_asr_result(self, text: str) -> bool:
        self.app_state.transcription_text = text
        if self.on_overlay_update:
            self.on_overlay_update(text)
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)
        if self.awaiting_final_result:
            self.awaiting_final_result = False
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    def _on_asr_finish(self) -> bool:
        self.awaiting_final_result = False
        if self.app_state.recording_state in (
            RecordingState.STOPPING,
            RecordingState.RECORDING,
        ):
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    def _on_asr_error(self, error) -> bool:
        if self.app_state.recording_state == RecordingState.IDLE:
            return GLib.SOURCE_REMOVE
        logger.error("ASR error: %s", error)
        # NOTE: genuine auth failures arrive via `on_auth_error` -> `_on_auth_error`,
        # which calls `_handle_auth_failure()` and re-prompts login. A generic ASR
        # error (connection refused, timeout, DNS, proxy down, etc.) must NOT be
        # treated as auth failure — doing so wipes cached cookies and pops the
        # login window every time the network/proxy is unavailable.
        self.app_state.error_message = "连接出错,请检查网络后重试"
        GLib.timeout_add(int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle)
        return GLib.SOURCE_REMOVE

    def _on_auth_error(self) -> bool:
        self._handle_auth_failure()
        return GLib.SOURCE_REMOVE

    # --- Completion & Reset ---

    def _complete_transcription(self) -> None:
        text = self.app_state.transcription_text.strip()
        logger.info("Completing transcription: '%s'", text[:50])
        if text and self.on_paste:
            self.on_paste(text)
        self._reset_to_idle()

    def _reset_to_idle(self) -> bool:
        self.awaiting_final_result = False
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._set_state(RecordingState.IDLE)
        self.app_state.error_message = None
        if self.on_overlay_hide:
            self.on_overlay_hide()
        self.using_cached_params = False
        # Clear text after short delay
        GLib.timeout_add(
            200, lambda: setattr(self.app_state, "transcription_text", "")
            or GLib.SOURCE_REMOVE
        )
        return GLib.SOURCE_REMOVE

    def handle_cancel(self) -> None:
        if self.app_state.recording_state == RecordingState.IDLE:
            return
        logger.info("Cancelling transcription")
        self.awaiting_final_result = False
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._reset_to_idle()

    def _handle_auth_failure(self) -> None:
        logger.warning("Auth failure, clearing cached params")
        ParamsStore.clear()
        self.using_cached_params = False
        self.audio_capture.stop()
        self.asr_client.disconnect()
        self._reset_to_idle()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self.on_auth_expired:
            self.on_auth_expired()

    def _set_state(self, new_state: RecordingState) -> None:
        self.app_state.recording_state = new_state
        if self.on_cancel_enabled_changed:
            self.on_cancel_enabled_changed(new_state != RecordingState.IDLE)

    def _on_params_extracted(self, params: ASRParams | None) -> None:
        """Called when WebView param extraction completes."""
        if params:
            ParamsStore.save(params)
            self.asr_client.connect(params)
        else:
            self.app_state.error_message = "无法获取连接参数，请重新登录"
            GLib.timeout_add(
                int(AUTH_EXPIRY_DELAY * 1000), self._reset_to_idle
            )

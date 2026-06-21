"""Application state management.

Mirrors AppState.swift.
"""

from __future__ import annotations

from enum import Enum

from gi.repository import GLib, GObject


class LoginStatus(Enum):
    CHECKING = "checking"
    LOGGED_IN = "logged_in"
    NOT_LOGGED_IN = "not_logged_in"


class RecordingState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"


class AppState(GObject.Object):
    """Shared application state, observable via GObject signals."""

    __gsignals__ = {
        "login-status-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "recording-state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "transcription-text-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "error-message-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._login_status = LoginStatus.CHECKING
        self._recording_state = RecordingState.IDLE
        self._transcription_text = ""
        self._error_message: str | None = None

    @property
    def login_status(self) -> LoginStatus:
        return self._login_status

    @login_status.setter
    def login_status(self, value: LoginStatus) -> None:
        if value != self._login_status:
            self._login_status = value
            self.emit("login-status-changed", value.value)

    @property
    def recording_state(self) -> RecordingState:
        return self._recording_state

    @recording_state.setter
    def recording_state(self, value: RecordingState) -> None:
        if value != self._recording_state:
            self._recording_state = value
            self.emit("recording-state-changed", value.value)

    @property
    def transcription_text(self) -> str:
        return self._transcription_text

    @transcription_text.setter
    def transcription_text(self, value: str) -> None:
        if value != self._transcription_text:
            self._transcription_text = value
            self.emit("transcription-text-changed", value)

    @property
    def error_message(self) -> str | None:
        return self._error_message

    @error_message.setter
    def error_message(self, value: str | None) -> None:
        if value != self._error_message:
            self._error_message = value
            self.emit("error-message-changed", value or "")

    @property
    def is_recording(self) -> bool:
        return self._recording_state in (
            RecordingState.RECORDING,
            RecordingState.STARTING,
        )

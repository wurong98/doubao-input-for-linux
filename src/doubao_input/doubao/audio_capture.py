"""Microphone capture via sounddevice (PortAudio/PipeWire).

Mirrors AudioCaptureManager.swift.

Key differences from macOS:
- sounddevice (PortAudio) replaces AVAudioEngine
- No resampling needed: PortAudio opens at 16kHz directly; PipeWire handles
  hardware rate adaptation internally
- No Float32->Int16 conversion needed: dtype='int16' gives native Int16 LE PCM
- RawInputStream delivers raw bytes, avoiding numpy overhead per callback
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from doubao_input.doubao.config import AUDIO_BLOCKSIZE, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE

logger = logging.getLogger(__name__)


class AudioCapture:
    """Captures microphone audio at 16kHz mono Int16 PCM."""

    def __init__(self) -> None:
        self._stream: Any | None = None
        self._on_audio_data = None  # callback: (bytes) -> None
        self._on_rms = None        # callback: (float in [0,1]) -> None
        self._lock = threading.Lock()

    @property
    def is_capturing(self) -> bool:
        return self._stream is not None and self._stream.active

    def start(self, on_audio_data) -> None:
        """Start capturing. on_audio_data(bytes) called on audio thread."""
        if self.is_capturing:
            return

        self._on_audio_data = on_audio_data

        sd = _load_sounddevice()
        # sounddevice with PortAudio uses PulseAudio compat on PipeWire
        self._stream = sd.RawInputStream(
            samplerate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            dtype="int16",
            blocksize=AUDIO_BLOCKSIZE,
            callback=self._audio_callback,
            latency="low",
        )
        self._stream.start()
        logger.info(
            "Audio capture started: %dHz, %d ch, int16",
            AUDIO_SAMPLE_RATE,
            AUDIO_CHANNELS,
        )

    def stop(self) -> None:
        """Stop capturing."""
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            self._on_audio_data = None
            self._on_rms = None
            logger.info("Audio capture stopped")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called by PortAudio on audio thread."""
        if status:
            logger.warning("Audio callback status: %s", status)
        # indata is already int16 LE bytes from RawInputStream
        data = bytes(indata)
        if self._on_audio_data:
            self._on_audio_data(data)
        if self._on_rms:
            # RMS in [0,1], normalized by 32768.
            # Compute on int16 view (cheap, ~256ms chunks at 16kHz).
            try:
                import array
                a = array.array("h")
                a.frombytes(data)
                if a:
                    s = 0
                    for v in a:
                        s += v * v
                    rms = (s / len(a)) ** 0.5 / 32768.0
                else:
                    rms = 0.0
            except Exception:
                rms = 0.0
            try:
                self._on_rms(min(1.0, rms))
            except Exception:
                pass

    @staticmethod
    def list_input_devices():
        """List available input devices for debugging."""
        sd = _load_sounddevice()
        return sd.query_devices(kind="input")

    @staticmethod
    def get_default_input_device():
        """Get the default input device index."""
        sd = _load_sounddevice()
        return sd.default.device[0]


def _load_sounddevice():
    try:
        import sounddevice as sd
    except Exception as e:
        raise RuntimeError(
            "sounddevice/PortAudio is not available; install sounddevice "
            "and PortAudio/PipeWire support before recording"
        ) from e
    return sd

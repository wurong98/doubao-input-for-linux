"""「检查麦克风」RMS 回调的回归测试。

背景: ``app._build()`` 会把 ``AudioCapture.start`` 换成带默认波形回调的
包装器。旧实现无条件把 ``_on_rms`` 写成 ``overlay.push_rms``,于是
``_check_mic()`` 事先挂上的统计回调被覆盖 —— 麦克风采集完全正常,
界面却永远显示「测试完成(无数据)」。

运行: ``PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v``
"""

from __future__ import annotations

import unittest

from doubao_input.app import _bind_capture_rms
from doubao_input.doubao import audio_capture


class _FakeCapture:
    """只实现 AudioCapture.start 的最小替身,避免依赖 PortAudio。"""

    def __init__(self) -> None:
        self._on_rms = None
        self._on_audio_data = None
        self.start_calls = 0

    def start(self, on_audio_data) -> None:
        self.start_calls += 1
        self._on_audio_data = on_audio_data


class MicRmsBindingTest(unittest.TestCase):
    def test_default_rms_is_pushed_to_overlay(self):
        cap = _FakeCapture()
        pushed: list[float] = []
        cap.start = _bind_capture_rms(cap, pushed.append)  # type: ignore[method-assign]

        cap.start(lambda _data: None)
        cap._on_rms(0.5)

        self.assertEqual(pushed, [0.5])

    def test_explicit_on_rms_is_not_overwritten(self):
        """麦克风检测传入的 on_rms 必须生效。旧实现下这里永远是空列表。"""
        cap = _FakeCapture()
        pushed: list[float] = []
        captured: list[float] = []
        cap.start = _bind_capture_rms(cap, pushed.append)  # type: ignore[method-assign]

        cap.start(lambda _data: None, on_rms=captured.append)
        cap._on_rms(0.42)

        self.assertEqual(captured, [0.42])
        self.assertEqual(pushed, [])

    def test_rms_callback_is_set_before_stream_starts(self):
        """先启动采集再挂回调会丢掉第一块音频。"""
        cap = audio_capture.AudioCapture()
        seen: dict[str, object] = {}

        class _FakeStream:
            def __init__(self, callback) -> None:
                self.callback = callback
                self.active = False

            def start(self) -> None:
                seen["on_rms"] = cap._on_rms
                self.active = True

            def stop(self) -> None:
                self.active = False

            def close(self) -> None:
                pass

        class _FakeSoundDevice:
            def RawInputStream(self, **kwargs):
                return _FakeStream(kwargs["callback"])

        original = audio_capture._load_sounddevice
        audio_capture._load_sounddevice = lambda: _FakeSoundDevice()
        try:
            cap.start = _bind_capture_rms(cap, lambda _r: None)  # type: ignore[method-assign]
            cap.start(on_audio_data=lambda _b: None, on_rms=lambda _r: None)
        finally:
            audio_capture._load_sounddevice = original

        self.assertIsNotNone(seen.get("on_rms"))


if __name__ == "__main__":
    unittest.main()

"""麦克风统计回归与关闭面板后仍可重新打开。"""
import unittest
from unittest.mock import MagicMock, patch

from doubao_input.app import DoubaoInputApp
from doubao_input.ui.control_window import ControlWindow


class BackgroundTests(unittest.TestCase):
    def test_microphone_test_callback_survives_start_wrapper(self):
        app = DoubaoInputApp()
        capture = MagicMock()
        capture._on_rms = None
        with patch('doubao_input.app.Overlay'), \
             patch('doubao_input.app.Injector'), \
             patch('doubao_input.app.TranscriptionManager'), \
             patch('doubao_input.app.ControlWindow'), \
             patch('doubao_input.app.EvdevPtt'), \
             patch('doubao_input.app.start_tray'), \
             patch('doubao_input.doubao.audio_capture.AudioCapture', return_value=capture):
            app._build()
        received = []
        callback = received.append
        capture._on_rms = callback
        capture.start(lambda _data: None)
        capture._on_rms(0.25)
        self.assertEqual(received, [0.25])
        capture._on_rms = None
        capture.start(lambda _data: None)
        self.assertEqual(capture._on_rms, app._overlay.push_rms)

    def test_paste_does_not_reopen_control_panel(self):
        app = DoubaoInputApp()
        app._overlay = MagicMock()
        app._control = MagicMock()
        app._control._window.get_visible.return_value = True
        app._injector = MagicMock()
        pending = []
        with patch('doubao_input.app.GLib.timeout_add', side_effect=lambda _ms, fn: pending.append(fn)):
            app._do_paste("测试文字")
            while pending:
                pending.pop(0)()
        app._control.hide.assert_called_once()
        app._control._window.set_visible.assert_not_called()
        app._injector.inject.assert_called_once()

    def test_background_start_hides_panel_but_explicit_activate_opens_it(self):
        app = DoubaoInputApp()
        app._control = MagicMock()
        with patch.object(app, '_build'), patch.object(app, 'hold'), \
             patch.dict('os.environ', {'DOUBAO_INPUT_BACKGROUND': '1'}), \
             patch('doubao_input.app.GLib.timeout_add'):
            app._on_activate(app)
            app._control.show.assert_not_called()
            app._on_activate(app)
            app._control.show.assert_called_once()

    def test_connected_state_updates_starting_message(self):
        from doubao_input.doubao.app_state import RecordingState
        app = DoubaoInputApp()
        app._overlay = MagicMock()
        app.app_state.recording_state = RecordingState.STARTING
        self.assertIsNotNone(app._startup_timer)
        app.app_state.recording_state = RecordingState.RECORDING
        self.assertIsNone(app._startup_timer)
        app._overlay.set_status.assert_called_with("正在聆听，请说话…")
        app.app_state.recording_state = RecordingState.STOPPING
        app._overlay.set_status.assert_called_with("正在识别…")
        with patch('doubao_input.app.GLib.idle_add') as idle:
            app.app_state.recording_state = RecordingState.IDLE
            idle.assert_called_once_with(app._show_feedback, "未识别到语音，请重试")

    def test_close_hides_and_reopen_keeps_same_window(self):
        control = ControlWindow(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        window = MagicMock()
        control._window = window
        self.assertTrue(control._on_close_request(window))
        window.set_visible.assert_called_once_with(False)
        window.destroy.assert_not_called()
        control.show()
        window.present.assert_called_once()
        self.assertIs(control._window, window)


if __name__ == '__main__':
    unittest.main()

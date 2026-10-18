"""粘贴快捷键回归测试；不访问真实剪贴板或发送按键。"""
import unittest
from unittest.mock import Mock, patch

from doubao_input.doubao.config import INJECT_USE_SHIFT
from doubao_input.inject.injector import (
    Injector, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V,
)


class PasteShortcutTests(unittest.TestCase):
    def test_default_shortcut_advertises_and_sends_shift(self):
        import evdev

        injector = Injector()
        with patch.object(evdev, "UInput") as device, patch(
            "doubao_input.inject.injector.time.sleep"
        ):
            self.assertTrue(INJECT_USE_SHIFT)
            self.assertTrue(injector.inject_via_uinput_only(INJECT_USE_SHIFT))
            supported = device.call_args.kwargs["events"][evdev.ecodes.EV_KEY]
            emitted = device.return_value.write.call_args_list
            self.assertEqual(
                [(call.args[1], call.args[2]) for call in emitted],
                [(KEY_LEFTCTRL, 1), (KEY_LEFTSHIFT, 1), (KEY_V, 1),
                 (KEY_V, 0), (KEY_LEFTSHIFT, 0), (KEY_LEFTCTRL, 0)],
            )
            self.assertTrue(all(call.args[1] in supported for call in emitted))

    def test_copy_failure_does_not_send_shortcut(self):
        injector = Injector()
        injector._copy_to_clipboard = Mock(return_value=False)
        injector._simulate_paste = Mock()
        self.assertFalse(injector.inject("测试", use_shift=INJECT_USE_SHIFT))
        injector._simulate_paste.assert_not_called()


if __name__ == "__main__":
    unittest.main()

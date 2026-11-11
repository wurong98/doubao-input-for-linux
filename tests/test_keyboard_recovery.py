import errno
import unittest
from unittest.mock import MagicMock, patch
from doubao_input.trigger.evdev_ptt import EvdevPtt

class KeyboardRecoveryTests(unittest.TestCase):
    def test_start_without_keyboard_keeps_recovery_thread(self):
        listener=EvdevPtt(lambda:None,lambda:None)
        with patch.object(listener,'_scan',return_value=False), patch('threading.Thread') as thread:
            self.assertFalse(listener.start())
            thread.return_value.start.assert_called_once()

    def test_periodic_scan_runs_with_existing_keyboard(self):
        listener=EvdevPtt(lambda:None,lambda:None)
        listener._fds=[99];listener._paths=['/fake']
        def select_once(*args):
            listener._stop.set();return ([],[],[])
        with patch.object(listener,'_scan',return_value=True) as scan, patch('select.select',side_effect=select_once):
            listener._run()
            scan.assert_called_once()

    def test_disconnected_fd_is_removed(self):
        listener=EvdevPtt(lambda:None,lambda:None)
        listener._fds=[99];listener._paths=['/fake']
        listener._last_rescan=10**20
        def disconnected(*args):
            listener._stop.set()
            raise OSError(errno.ENODEV,'No such device')
        with patch('select.select',return_value=([99],[],[])), patch('os.read',side_effect=disconnected),patch('os.close'):
            listener._run()
        self.assertEqual(listener._fds,[])
        self.assertEqual(listener._paths,[])

if __name__=='__main__':unittest.main()

import unittest
import threading
import time
from unittest.mock import Mock, patch
from codex_monitor import Window


class DisplayRecoveryTests(unittest.TestCase):
    def window(self, x=1848, y=626, area=(0, 0, 1920, 1040)):
        w = Window.__new__(Window)
        w.dragging = False
        w.follow_codex = True
        w.app_available = True
        w.dock_side = "right"
        w.dock_y = y
        w.screen_signature = (area, 96.0)
        w.last_visibility_check = 0
        w.last_heartbeat = time.monotonic()
        w.root = Mock()
        w.root.winfo_fpixels.return_value = 96
        w.root.winfo_width.return_value = 72
        w.root.winfo_height.return_value = 64
        w.root.winfo_x.return_value = x
        w.root.winfo_y.return_value = y
        w.root.state.return_value = "normal"
        w.work_area = Mock(return_value=area)
        return w

    def test_resolution_shrink_brings_old_right_edge_back(self):
        w = self.window()
        w.work_area.return_value = (0, 0, 1280, 680)
        w.ensure_visible()
        # Same vertical proportion, new right edge, entirely inside the work area.
        w.root.geometry.assert_called_once_with("72x64+1208+395")
        w.root.attributes.assert_called_with("-topmost", True)

    def test_left_side_survives_change_and_vertical_position_is_clamped(self):
        w = self.window(x=0, y=1800)
        w.dock_side = "left"
        w.work_area.return_value = (0, 0, 1280, 680)
        w.ensure_visible()
        w.root.geometry.assert_called_once_with("72x64+0+616")

    def test_offscreen_is_recovered_without_a_resolution_event(self):
        w = self.window(x=2400, y=-200)
        w.ensure_visible()
        w.root.geometry.assert_called_once_with("72x64+1848+0")

    def test_dpi_change_reanchors_window(self):
        w = self.window()
        w.root.winfo_fpixels.return_value = 144
        w.ensure_visible()
        w.root.geometry.assert_called_once_with("72x64+1848+626")

    def test_normal_position_is_not_continuously_moved(self):
        w = self.window()
        w.ensure_visible()
        w.root.geometry.assert_not_called()

    def test_topmost_is_periodically_reasserted_without_moving_window(self):
        w = self.window()
        w.last_visibility_check = 10
        w.last_heartbeat = 10
        with patch("codex_monitor.time.monotonic", return_value=16):
            w.ensure_visible()
        w.root.attributes.assert_called_once_with("-topmost", True)
        w.root.geometry.assert_not_called()

    def test_does_not_interrupt_dragging(self):
        w = self.window(x=2500)
        w.dragging = True
        w.ensure_visible()
        w.root.geometry.assert_not_called()

    def test_intentionally_hidden_follow_mode_stays_hidden(self):
        w = self.window()
        w.app_available = False
        w.root.state.return_value = "withdrawn"
        w.ensure_visible()
        w.root.deiconify.assert_not_called()
        w.root.geometry.assert_not_called()

    def test_unexpected_hidden_window_is_restored(self):
        w = self.window()
        w.root.state.return_value = "withdrawn"
        w.ensure_visible()
        w.root.deiconify.assert_called_once()
        w.root.geometry.assert_called_once_with("72x64+1848+626")

    def test_full_panel_collapses_when_small_display_cannot_fit_it(self):
        w = self.window()
        w.work_area.return_value = (0, 0, 1280, 360)
        w.root.winfo_width.return_value = 320
        w.root.winfo_height.return_value = 500
        w.detail = Mock()
        w.detail.get.return_value = True
        def collapse(expanded):
            w.root.winfo_width.return_value = 72
            w.root.winfo_height.return_value = 64
        w.set_view = Mock(side_effect=collapse)
        w.ensure_visible()
        w.set_view.assert_called_once_with(False)
        self.assertTrue(w.root.geometry.called)


class FollowAndRefreshTests(unittest.TestCase):
    def window(self):
        w = Window.__new__(Window)
        w.root = Mock()
        w.stop = threading.Event()
        w.app_available = True
        w.app_absent_samples = 0
        w.ensure_visible = Mock()
        return w

    def test_transient_missing_process_does_not_hide_paw(self):
        w = self.window()
        for available in (False, False, True):
            w.observe_app_available(available)
        w.root.withdraw.assert_not_called()
        self.assertTrue(w.app_available)

    def test_confirmed_exit_hides_and_restart_restores_paw(self):
        w = self.window()
        for _ in range(3):
            w.observe_app_available(False)
        w.root.withdraw.assert_called_once()
        self.assertFalse(w.app_available)
        w.observe_app_available(True)
        w.root.deiconify.assert_called_once()
        self.assertTrue(w.app_available)
        w.ensure_visible.assert_called_once_with(force=True)

    def test_unknown_breaks_absence_streak(self):
        w = self.window()
        for available in (False, False, None, False, False):
            w.observe_app_available(available)
        w.root.withdraw.assert_not_called()

    def test_failed_show_is_retried_on_next_observation(self):
        w = self.window()
        w.app_available = False
        w.root.deiconify.side_effect = [RuntimeError("display transition"), None]
        with self.assertRaises(RuntimeError):
            w.observe_app_available(True)
        self.assertFalse(w.app_available)
        w.observe_app_available(True)
        self.assertTrue(w.app_available)
        self.assertEqual(w.root.deiconify.call_count, 2)

    def test_refresh_exception_still_recovers_visibility_and_schedules_next_tick(self):
        w = self.window()
        w.refresh = Mock(side_effect=RuntimeError("temporary UI error"))
        with patch("codex_monitor.log_exception") as log:
            w.tick()
        log.assert_called_once()
        w.ensure_visible.assert_called_once()
        w.root.after.assert_called_once_with(500, w.tick)

    def test_recovery_exception_does_not_stop_future_ticks(self):
        w = self.window()
        w.refresh = Mock()
        w.ensure_visible.side_effect = RuntimeError("temporary display error")
        with patch("codex_monitor.log_exception"):
            w.tick()
        w.root.after.assert_called_once_with(500, w.tick)

    def test_close_during_refresh_does_not_reschedule(self):
        w = self.window()
        w.refresh = Mock(side_effect=w.stop.set)
        w.tick()
        w.ensure_visible.assert_not_called()
        w.root.after.assert_not_called()


class ProcessDetectionTests(unittest.TestCase):
    def test_failed_enumeration_is_unknown_instead_of_absent(self):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 42
        kernel.Process32FirstW.return_value = False
        with patch("codex_monitor.ctypes.WinDLL", return_value=kernel), \
                patch("codex_monitor.ctypes.get_last_error", return_value=5):
            from codex_monitor import codex_running
            self.assertIsNone(codex_running(desktop_only=True))
        kernel.CloseHandle.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()

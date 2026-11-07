import unittest
from unittest.mock import Mock
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


if __name__ == "__main__":
    unittest.main()

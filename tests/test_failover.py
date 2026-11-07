from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock, mock_open, patch
import uuid

from clash_failover import ClashClient, FailoverGuard, is_japan_node
from codex_monitor import Window


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).parent / ("guard-" + uuid.uuid4().hex + ".json")
        self.guard = FailoverGuard(self.path)
        self.context = ("task", 500, 800)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def observe(self, now, ok=False, context="default", kind="connection"):
        return self.guard.observe({"ok": ok, "at": now, "kind": kind}, self.context if context == "default" else context, now)

    def test_requires_three_new_failures(self):
        self.assertFalse(self.observe(1000))
        self.assertFalse(self.observe(1030))
        self.assertTrue(self.observe(1060))

    def test_success_unknown_certificate_or_completion_resets(self):
        for ok, kind, context in ((True, "http", "default"), (None, "http", "default"),
                                  (False, "certificate", "default"), (False, "connection", None)):
            self.observe(1000)
            self.observe(1030)
            self.assertFalse(self.observe(1060, ok, context, kind))
            self.assertEqual(self.guard.streak, 0)

    def test_progress_and_probe_gaps_reset(self):
        self.observe(1000)
        self.observe(1030)
        self.assertFalse(self.observe(1060, context=("task", 500, 1040)))
        self.assertEqual(self.guard.streak, 1)
        self.assertFalse(self.observe(1200))
        self.assertEqual(self.guard.streak, 1)

    def test_cooldown_hourly_limit_and_restart(self):
        self.guard.reserve(1000)
        self.assertFalse(self.observe(1030))
        self.assertFalse(self.observe(1060))
        self.assertFalse(self.observe(1090))
        restored = FailoverGuard(self.path)
        self.assertEqual(restored.attempts, [1000])
        self.guard.attempts = [800, 900, 1000]
        self.assertFalse(self.observe(1300))

    def test_stale_or_before_progress_samples_rejected(self):
        for at in (700, 980, 1100):
            self.assertFalse(self.guard.observe({"ok": False, "at": at}, self.context, 1000))


class SwitchingTests(unittest.TestCase):
    def setUp(self):
        self.window = Window.__new__(Window)
        self.window.stop = threading.Event()
        self.window.switch_enabled = self.window.probe_enabled = True
        self.window.switch_context = ("task", 500, 800)
        self.window.updates = queue.Queue()
        self.client = Mock()
        self.client.proxy_port = 7897
        self.client.plan.return_value = {"group": "AI", "old": "old", "current_leaf": "old", "candidates": ["new"]}
        self.client.choose.return_value = "new"
        self.client.unchanged.return_value = True
        self.client.current.return_value = "new"

    def run_switch(self, result=None):
        with patch("codex_monitor.probe", return_value=result or {"ok": None}), patch("pathlib.Path.open", mock_open()):
            self.window.perform_failover(self.client, Mock(), self.window.switch_context)

    def test_switches_ai_group_after_valid_candidate(self):
        self.run_switch()
        self.client.select.assert_called_once_with("AI", "new")

    def test_recovery_during_delay_test_cancels(self):
        def candidate(plan, allowed):
            self.window.switch_context = None
            return "new"
        self.client.choose.side_effect = candidate
        self.run_switch()
        self.client.select.assert_not_called()

    def test_manual_selection_cancels(self):
        self.client.unchanged.return_value = False
        self.run_switch()
        self.client.select.assert_not_called()

    def test_failed_postcheck_rolls_back(self):
        self.run_switch({"ok": False})
        self.assertEqual([c.args for c in self.client.select.call_args_list], [("AI", "new"), ("AI", "old")])

    def test_postcheck_preserves_manual_selection(self):
        self.client.current.return_value = "manual"
        self.run_switch({"ok": False})
        self.client.select.assert_called_once_with("AI", "new")

    def test_never_sends_controller_secret_to_remote(self):
        with self.assertRaises(ValueError):
            ClashClient("8.8.8.8:9097", "secret")

    def test_nested_selection_resolves_leaf(self):
        proxies = {"AI": {"all": ["Group"], "now": "Group"}, "Group": {"all": ["node"], "now": "node"}, "node": {"type": "Vmess"}}
        self.assertEqual(ClashClient.leaf(proxies, "AI"), "node")

    def test_japan_names_only(self):
        for name in ("🇯🇵AWS日本02", "日本东京05", "JP01", "Japan 02", "Tokyo-03", "大阪"):
            self.assertTrue(is_japan_node(name), name)
        for name in ("🇸🇬新加坡01", "美国01", "Singapore", "🇺🇸日本中转", "自动选择"):
            self.assertFalse(is_japan_node(name), name)

    def test_plan_excludes_foreign_nodes_current_and_groups(self):
        client = ClashClient("127.0.0.1:9097")
        client.target = Mock(return_value="AI")
        client.request = Mock(return_value={"proxies": {
            "AI": {"type": "Selector", "now": "日本01", "all": ["日本01", "日本02", "新加坡01", "日本自动", "🇺🇸日本中转"]},
            "日本01": {"type": "Vless"}, "日本02": {"type": "Vless"},
            "新加坡01": {"type": "Vless"}, "日本自动": {"type": "URLTest", "all": ["日本02"]},
            "🇺🇸日本中转": {"type": "Vless"}}})
        self.assertEqual(client.plan()["candidates"], ["日本02"])

    def test_no_japan_candidates_does_not_fall_back_to_other_regions(self):
        client = ClashClient("127.0.0.1:9097")
        client.request = Mock()
        self.assertIsNone(client.choose({"candidates": ["新加坡01", "美国01"]}, lambda: True))
        client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import uuid
import unittest
from codex_monitor import Session, classify


def record(kind, payload, second=0):
    return {"timestamp": f"2026-11-01T00:00:{second:02d}Z", "type": kind, "payload": payload}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.s = Session("test", Path("unused"))
        self.s.consume(record("event_msg", {"type": "task_started"}))
        self.start = self.s.last_activity

    def test_silence_warns_without_proven_fault(self):
        self.assertIn("持续无新活动", classify(self.s, self.start + 61)[0])
        self.assertIn("长时间无新活动", classify(self.s, self.start + 181)[0])

    def test_completed_and_aborted_are_idle(self):
        for kind in ("task_complete", "turn_aborted"):
            self.s.consume(record("event_msg", {"type": kind}, 5))
            self.assertIn("空闲", classify(self.s, self.start + 999)[0])

    def test_bookkeeping_does_not_hide_silence(self):
        self.s.consume(record("event_msg", {"type": "token_count"}, 59))
        self.s.consume(record("token_usage_record", {}, 59))
        self.assertEqual(self.s.last_activity, self.start)

    def test_tool_wait_can_be_stale_then_recover(self):
        self.s.consume(record("response_item", {"type": "custom_tool_call", "call_id": "a", "name": "exec"}, 2))
        self.assertTrue(self.s.pending)
        self.assertIn("长时间无新活动", classify(self.s, self.start + 200)[0])
        self.s.consume(record("response_item", {"type": "custom_tool_call_output", "call_id": "a"}, 8))
        self.assertFalse(self.s.pending)
        self.assertEqual(classify(self.s, self.start + 9)[0], "运行中")

    def test_error_only_from_structured_event_not_tool_text(self):
        self.s.consume(record("response_item", {"type": "function_call_output", "output": "proxy timeout"}, 5))
        self.assertEqual(self.s.network_error_at, 0)
        self.s.consume(record("event_msg", {"type": "error", "message": "stream disconnected"}, 6))
        self.assertIn("连接错误", classify(self.s, self.start + 70)[0])

    def test_network_failure_only_changes_silent_running_task(self):
        self.assertEqual(classify(self.s, self.start + 2, probe_failed=True)[0], "运行中")
        self.assertIn("网络探测失败", classify(self.s, self.start + 70, probe_failed=True)[0])

    def test_incremental_partial_record_and_truncation(self):
        path = Path(__file__).resolve().parent / ("fixture-" + uuid.uuid4().hex + ".jsonl")
        try:
            line = json.dumps(record("event_msg", {"type": "task_started"})).encode()
            path.write_bytes(line[:30])
            s = Session("x", path)
            s.read()
            self.assertIsNone(s.active)
            with path.open("ab") as f:
                f.write(line[30:] + b"\n")
            s.read()
            self.assertTrue(s.active)
            offset = s.offset
            s.read()
            self.assertEqual(s.offset, offset)
            path.write_bytes(b"{}\n")
            s.read()
            self.assertIsNone(s.active)
            self.assertEqual(s.last_activity, 0)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

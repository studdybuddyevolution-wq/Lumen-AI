from __future__ import annotations

import unittest

from backtalk.execution import ExecutionTracker


class ExecutionTrackerTests(unittest.TestCase):
    def test_running_call_finishes_with_latency_and_snapshot(self) -> None:
        tracker = ExecutionTracker(max_events=5)
        arguments = {"path": "C:/Lumen/note.md", "text": "hello"}
        execution_id = tracker.start("write_file", arguments)
        arguments["path"] = "changed"

        running = tracker.recent(1)[0]
        self.assertEqual(running["id"], execution_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["arguments"]["path"], "C:/Lumen/note.md")
        self.assertEqual(running["provider"], "local")

        finished = tracker.finish(execution_id, "completed", "File saved.")
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["result"], "File saved.")
        self.assertIsInstance(finished["latency_ms"], float)
        self.assertGreaterEqual(finished["latency_ms"], 0)

    def test_terminal_statuses_are_recorded(self) -> None:
        tracker = ExecutionTracker()
        for status in ("failed", "denied"):
            execution_id = tracker.start("example_tool", {})
            tracker.finish(execution_id, status, f"{status} result")

        self.assertEqual(
            [event["status"] for event in tracker.recent(2)],
            ["denied", "failed"],
        )

    def test_history_is_bounded_and_newest_first(self) -> None:
        tracker = ExecutionTracker(max_events=2)
        first = tracker.start("first", {})
        second = tracker.start("second", {})
        third = tracker.start("third", {})

        events = tracker.recent(10)
        self.assertEqual([event["id"] for event in events], [third, second])
        self.assertNotIn(first, [event["id"] for event in events])

    def test_payloads_are_truncated_and_status_is_validated(self) -> None:
        tracker = ExecutionTracker(max_payload_chars=256)
        execution_id = tracker.start("large_tool", {"text": "x" * 1000})
        event = tracker.recent(1)[0]
        self.assertTrue(event["arguments"]["_truncated"])

        with self.assertRaisesRegex(ValueError, "status must be one of"):
            tracker.finish(execution_id, "pending", "not terminal")


if __name__ == "__main__":
    unittest.main()

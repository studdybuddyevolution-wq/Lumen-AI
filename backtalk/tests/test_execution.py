from __future__ import annotations

import asyncio
import unittest

from backtalk.brain import WarmBrain
from backtalk.execution import ExecutionTracker
from backtalk.registry import ToolValidationError


class ExecutionTrackerTests(unittest.TestCase):
    def test_running_call_finishes_with_latency_and_snapshot(self) -> None:
        tracker = ExecutionTracker(max_events=5)
        arguments = {"path": "C:/Lumen/note.md", "text": "hello"}
        execution_id = tracker.start("write_file", arguments)
        arguments["path"] = "changed"

        running = tracker.recent(1)[0]
        self.assertEqual(running["id"], execution_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["args"]["path"], "C:/Lumen/note.md")
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
        self.assertTrue(event["args"]["_truncated"])

        with self.assertRaisesRegex(ValueError, "status must be one of"):
            tracker.finish(execution_id, "pending", "not terminal")


class _StubRegistry:
    def __init__(self, *, known: bool = True, result: str = "ok", error=None):
        self.known = known
        self.result = result
        self.error = error

    def get(self, name):
        return object() if self.known else None

    def execute(self, name, arguments):
        if self.error is not None:
            raise self.error
        return self.result


class ToolExecutionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.brain = WarmBrain.__new__(WarmBrain)
        self.brain.execution_tracker = ExecutionTracker()
        self.brain._registry = _StubRegistry()

    async def test_success_records_completed_call(self) -> None:
        result = await self.brain._run_tool({
            "function": {"name": "sample_tool", "arguments": {"value": 7}}
        })
        event = self.brain.execution_tracker.recent(1)[0]
        self.assertEqual(result, "ok")
        self.assertEqual(event["name"], "sample_tool")
        self.assertEqual(event["args"], {"value": 7})
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["result"], "ok")

    async def test_unknown_tool_is_recorded_as_denied(self) -> None:
        self.brain._registry = _StubRegistry(known=False)
        result = await self.brain._run_tool({
            "function": {"name": "missing_tool", "arguments": {}}
        })
        event = self.brain.execution_tracker.recent(1)[0]
        self.assertIn("not available", result)
        self.assertEqual(event["status"], "denied")

    async def test_invalid_arguments_are_recorded_as_denied(self) -> None:
        self.brain._registry = _StubRegistry(error=ToolValidationError("value is invalid"))
        result = await self.brain._run_tool({
            "function": {"name": "sample_tool", "arguments": {"value": 7}}
        })
        event = self.brain.execution_tracker.recent(1)[0]
        self.assertIn("Tool rejected", result)
        self.assertEqual(event["status"], "denied")

    async def test_handler_exception_is_recorded_as_failed(self) -> None:
        self.brain._registry = _StubRegistry(error=RuntimeError("simulated failure"))
        result = await self.brain._run_tool({
            "function": {"name": "sample_tool", "arguments": {}}
        })
        event = self.brain.execution_tracker.recent(1)[0]
        self.assertIn("simulated failure", result)
        self.assertEqual(event["status"], "failed")


if __name__ == "__main__":
    unittest.main()

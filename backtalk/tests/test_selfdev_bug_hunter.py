from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backtalk import bridge
from backtalk.selfdev.bug_hunter import BugHunter, redact_text


class BugHunterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.py"
        self.source.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        api_key = 'hardcoded-value-should-not-leak'\n"
            "        raise RuntimeError('failure')\n"
            "    inner()\n"
            "\n"
            "outer()\n",
            encoding="utf-8",
        )
        self.hunter = BugHunter(self.root, max_reports=3)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_traceback(
        self,
        *,
        path: Path | None = None,
        line: int = 4,
        function: str = "inner",
        message: str = "failure 123",
    ) -> str:
        target = path or self.source
        return (
            "Traceback (most recent call last):\n"
            f'  File "{target}", line 1, in outer\n'
            f'  File "{target}", line {line}, in {function}\n'
            "    raise RuntimeError('failure')\n"
            f"RuntimeError: {message}\n"
        )

    def test_extracts_innermost_project_frame_and_ast_symbol(self) -> None:
        report = self.hunter.capture_traceback(self.make_traceback())

        self.assertEqual(report.primary_file, "sample.py")
        self.assertEqual(report.primary_line, 4)
        self.assertEqual(report.primary_function, "inner")
        self.assertEqual(report.ast_symbol, "inner")
        self.assertEqual(report.frames[-1].file, "sample.py")
        self.assertIn("raise RuntimeError", "\n".join(report.source_context))

    def test_outside_project_frames_are_not_recorded(self) -> None:
        outside = self.root.parent / "outside_private.py"
        report = self.hunter.capture_traceback(self.make_traceback(path=outside))

        self.assertIsNone(report.primary_file)
        self.assertEqual(report.frames, ())
        self.assertNotIn(str(self.root), str(report.to_dict()))

    def test_secrets_are_redacted_from_message_and_source_context(self) -> None:
        report = self.hunter.capture_traceback(
            self.make_traceback(message="Authorization: Bearer super-secret-token"),
            message="api_key=message-secret password='quoted secret' "
                    "Authorization: Bearer auth-secret",
        )
        combined = report.message + "\n" + "\n".join(report.source_context)

        for secret in (
            "message-secret",
            "quoted secret",
            "auth-secret",
            "hardcoded-value-should-not-leak",
            "super-secret-token",
        ):
            self.assertNotIn(secret, combined)
        self.assertIn("[redacted]", combined)

    def test_redacts_bearer_tokens_private_keys_and_home_paths(self) -> None:
        redacted = redact_text(
            "Authorization: Bearer abcdef123 password='with spaces' "
            "C:\\Users\\Arin\\Lumen\\app.py "
            "-----BEGIN PRIVATE KEY-----secret-material-----END PRIVATE KEY-----"
        )

        self.assertNotIn("abcdef123", redacted)
        self.assertNotIn("with spaces", redacted)
        self.assertNotIn("C:\\Users\\Arin", redacted)
        self.assertNotIn("secret-material", redacted)
        self.assertIn("%USERPROFILE%", redacted)

    def test_repeated_failures_are_deduplicated_and_counted(self) -> None:
        first = self.hunter.capture_traceback(self.make_traceback(message="failure 123"))
        second = self.hunter.capture_traceback(self.make_traceback(message="failure 456"))

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.occurrence_count, 2)
        summary = self.hunter.summary()
        self.assertEqual(summary["events_seen"], 2)
        self.assertEqual(summary["unique_reports"], 1)
        self.assertEqual(summary["recurrent_reports"], 1)

    def test_report_history_is_bounded(self) -> None:
        for label in ("alpha", "bravo", "charlie", "delta", "echo"):
            self.hunter.capture_traceback(
                self.make_traceback(message=f"failure-kind-{label}")
            )

        self.assertEqual(len(self.hunter.recent(10)), 3)
        self.assertEqual(self.hunter.summary()["events_seen"], 5)

    def test_capture_exception_formats_traceback_without_retaining_it(self) -> None:
        try:
            self.source.write_text(
                "def fail_now():\n"
                "    raise ValueError('api_key=do-not-store')\n"
                "fail_now()\n",
                encoding="utf-8",
            )
            exec(compile(self.source.read_text(encoding="utf-8"), str(self.source), "exec"), {})
        except ValueError as exc:
            report = self.hunter.capture_exception(exc, component="test:runtime")

        self.assertEqual(report.exception_type, "ValueError")
        self.assertEqual(report.primary_file, "sample.py")
        self.assertEqual(report.primary_function, "fail_now")
        self.assertNotIn("do-not-store", str(report.to_dict()))
        self.assertEqual(report.component, "test:runtime")

    def test_syntax_error_still_includes_bounded_source_context(self) -> None:
        self.source.write_text(
            "def broken(:\n"
            "    return 1\n",
            encoding="utf-8",
        )
        report = self.hunter.capture_traceback(
            self.make_traceback(line=1, function="<module>"),
            exception_type="SyntaxError",
            message="invalid syntax",
        )

        self.assertEqual(report.primary_file, "sample.py")
        self.assertIsNone(report.ast_symbol)
        self.assertIn("def broken", "\n".join(report.source_context))

    def test_report_snapshots_do_not_expose_mutable_internal_state(self) -> None:
        self.hunter.capture_traceback(self.make_traceback())
        snapshot = self.hunter.recent(1)
        snapshot[0]["frames"].clear()
        snapshot[0]["source_context"].clear()

        next_snapshot = self.hunter.recent(1)[0]
        self.assertTrue(next_snapshot["frames"])
        self.assertTrue(next_snapshot["source_context"])

    def test_bridge_returns_bug_summary_without_exposing_raw_traceback(self) -> None:
        self.hunter.capture_traceback(self.make_traceback())
        original_brain = bridge._brain
        bridge._brain = SimpleNamespace(bug_hunter=self.hunter)
        try:
            payload = bridge._get_bugs(10)
        finally:
            bridge._brain = original_brain

        self.assertEqual(payload["events_seen"], 1)
        self.assertEqual(payload["unique_reports"], 1)
        self.assertEqual(len(payload["reports"]), 1)
        self.assertNotIn(str(self.root), str(payload))


if __name__ == "__main__":
    unittest.main()

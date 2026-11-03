from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from backtalk.selfdev.sandbox import SandboxError, SandboxRunner


@unittest.skipIf(os.environ.get('LUMEN_SANDBOX') == '1', 'avoid recursively launching the sandbox from its own test suite')
class SandboxRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        (self.root / "tests").mkdir(parents=True)

    def test_checks_run_in_a_disposable_copy_not_the_source_tree(self) -> None:
        test_file = self.root / "tests" / "test_write.py"
        test_file.write_text(
            "from pathlib import Path\n"
            "import unittest\n"
            "class SnapshotTests(unittest.TestCase):\n"
            "    def test_writes_only_to_snapshot(self):\n"
            "        Path('sandbox-marker.txt').write_text('sandbox')\n"
            "        self.assertTrue(Path('sandbox-marker.txt').exists())\n",
            encoding="utf-8",
        )

        receipt = SandboxRunner().run_checks(
            self.root, checks=("compileall", "unittest"), timeout_seconds=30
        )

        self.assertTrue(receipt.passed, [check.stderr for check in receipt.checks])
        self.assertFalse((self.root / "sandbox-marker.txt").exists())
        self.assertEqual(receipt.isolation, "temporary-copy-subprocess")
        self.assertFalse(receipt.security_boundary)

    def test_failed_test_produces_failed_receipt(self) -> None:
        (self.root / "tests" / "test_failure.py").write_text(
            "import unittest\n"
            "class FailureTests(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            "        self.fail('expected failure')\n",
            encoding="utf-8",
        )

        receipt = SandboxRunner().run_checks(
            self.root, checks=("unittest",), timeout_seconds=30
        )

        self.assertFalse(receipt.passed)
        self.assertEqual(receipt.checks[0].exit_code, 1)
        self.assertIn("expected failure", receipt.checks[0].stderr + receipt.checks[0].stdout)

    def test_receipt_binds_to_subject_file_digest(self) -> None:
        subject = self.root / "feature.py"
        subject.write_text("VALUE = 42\n", encoding="utf-8")
        (self.root / "tests" / "test_ok.py").write_text(
            "import unittest\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )

        receipt = SandboxRunner().run_checks(
            self.root, checks=("compileall", "unittest"), subject_path=subject
        )

        self.assertTrue(receipt.passed)
        self.assertEqual(receipt.subject_path, "feature.py")
        self.assertEqual(
            receipt.subject_sha256,
            hashlib.sha256(subject.read_bytes()).hexdigest(),
        )

    def test_rejects_unsupported_checks_and_paths_outside_project(self) -> None:
        with self.assertRaises(SandboxError):
            SandboxRunner().run_checks(self.root, checks=("powershell",))
        with self.assertRaises(SandboxError):
            SandboxRunner().run_checks(
                self.root,
                checks=("compileall",),
                subject_path=Path(self.temp.name) / "outside.py",
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import tempfile
import unittest
import uuid
from pathlib import Path

from backtalk.selfdev.archive import (
    ArchiveError,
    CandidateArchive,
    CandidateEvaluationController,
)
from backtalk.selfdev.sandbox import (
    SandboxCheckResult,
    SandboxReceipt,
    SandboxRunner,
    project_tree_digest,
    _capture_stream,
)

SUITE = "lumen-capabilities-v1"
METRICS_A = {
    "task_success_rate": 0.90,
    "tool_routing_accuracy": 0.90,
    "schema_validity_rate": 0.90,
    "median_latency_ms": 100,
}
SAFETY_PASS = {
    "security_policy_passed": True,
    "policy_regressions": 0,
    "checks": {"workspace_boundary": True, "approval_gate": True},
}


class StubSandboxRunner:
    """Deterministic test double that returns digest-bound check receipts."""

    def __init__(self, fail_check: str | None = None) -> None:
        self.fail_check = fail_check
        self.calls: list[tuple[str, ...]] = []

    def run_checks(
        self,
        source_dir: str | Path,
        *,
        checks: tuple[str, ...],
        timeout_seconds: int = 180,
    ) -> SandboxReceipt:
        self.calls.append(checks)
        passed = self.fail_check not in checks
        results = tuple(
            SandboxCheckResult(
                name=name,
                passed=passed,
                exit_code=0 if passed else 1,
                timed_out=False,
                duration_ms=2,
                stdout="",
                stderr="",
            )
            for name in checks
        )
        digest = project_tree_digest(source_dir)
        return SandboxReceipt(
            receipt_id=uuid.uuid4().hex,
            source_sha256=digest,
            snapshot_sha256=digest,
            subject_path=None,
            subject_sha256=None,
            passed=passed,
            checks=results,
            started_at="2026-11-03T00:00:00+00:00",
            finished_at="2026-11-03T00:00:01+00:00",
            duration_ms=2,
            isolation="test-double",
            security_boundary=False,
            warnings=(),
        )


class CandidateArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
        tests = self.project / "tests"
        tests.mkdir()
        (tests / "test_feature.py").write_text(
            "import unittest\n"
            "class FeatureTests(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(42, 42)\n",
            encoding="utf-8",
        )
        self.archive = CandidateArchive(self.root / "evolution-archive")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_candidate(
        self,
        *,
        branch: str = "feature/experiment",
        parent: str | None = None,
        project: Path | None = None,
    ):
        return self.archive.add_candidate(
            project or self.project,
            source_branch=branch,
            source_commit="a" * 40,
            parent_candidate_id=parent,
            improvement_summary="Improve one general tool-routing capability.",
            changed_files=("backtalk/tool.py", "tests/test_tool.py"),
            suite_version=SUITE,
        )

    def evaluate(
        self,
        candidate_id: str,
        *,
        runner=None,
        metrics=METRICS_A,
        safety=SAFETY_PASS,
    ):
        controller = CandidateEvaluationController(
            self.archive,
            sandbox_runner=runner or StubSandboxRunner(),
        )
        return controller.evaluate(
            candidate_id,
            self.project,
            suite_version=SUITE,
            benchmark_metrics=metrics,
            safety_results=safety,
            timeout_seconds=30,
        )

    def test_candidate_manifest_is_digest_bound_and_lists_changed_files(self) -> None:
        candidate = self.add_candidate()

        self.assertEqual(candidate.source_sha256, project_tree_digest(self.project))
        self.assertEqual(candidate.changed_files, ("backtalk/tool.py", "tests/test_tool.py"))
        self.assertEqual(self.archive.get_candidate(candidate.candidate_id), candidate)
        self.assertEqual(self.archive.list_candidates(), [candidate])

    def test_candidate_metadata_is_optional_and_round_trips_as_json(self) -> None:
        expected_id = uuid.uuid4().hex
        candidate = self.archive.add_candidate(
            self.project,
            source_branch="feature/metadata",
            improvement_summary="Record additional candidate audit context.",
            suite_version=SUITE,
            metadata={"skill_forge": {"worktree_ref": expected_id, "schema_version": 1}},
            candidate_id=expected_id,
        )

        self.assertEqual(candidate.candidate_id, expected_id)
        self.assertEqual(candidate.metadata["skill_forge"]["schema_version"], 1)
        self.assertEqual(
            self.archive.get_candidate(candidate.candidate_id).metadata,
            candidate.metadata,
        )

    def test_archive_must_be_outside_candidate_source_tree(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "outside"):
            CandidateArchive(self.project / "archive").add_candidate(
                self.project,
                source_branch="feature/inside",
                improvement_summary="Invalid archive placement.",
                suite_version=SUITE,
            )

    def test_rejects_unsafe_branch_and_changed_file_paths(self) -> None:
        with self.assertRaises(ArchiveError):
            self.add_candidate(branch="../main")
        with self.assertRaises(ArchiveError):
            self.archive.add_candidate(
                self.project,
                source_branch="feature/safe",
                improvement_summary="Bad path",
                changed_files=("../outside.py",),
                suite_version=SUITE,
            )

    def test_child_requires_a_retained_passing_parent(self) -> None:
        parent = self.add_candidate()
        with self.assertRaisesRegex(ArchiveError, "retained, passing parent"):
            self.add_candidate(branch="feature/child", parent=parent.candidate_id)

        self.evaluate(parent.candidate_id)
        self.archive.record_decision(
            parent.candidate_id, decision="retain", reason="Baseline passed evaluation."
        )
        child = self.add_candidate(branch="feature/child", parent=parent.candidate_id)
        self.assertEqual(child.parent_candidate_id, parent.candidate_id)

    def test_evaluation_runs_compile_smoke_then_full_regression_and_records_metrics(self) -> None:
        candidate = self.add_candidate()
        runner = StubSandboxRunner()

        result = self.evaluate(candidate.candidate_id, runner=runner)

        self.assertTrue(result.passed)
        self.assertIsNone(result.failure_stage)
        self.assertEqual(runner.calls, [("compileall",), ("compileall", "unittest")])
        self.assertEqual([item.name for item in result.smoke_checks], ["compileall"])
        self.assertEqual(
            [item.name for item in result.regression_checks],
            ["compileall", "unittest"],
        )
        self.assertEqual(result.benchmark_metrics["task_success_rate"], 0.9)
        self.assertTrue(result.safety_results["security_policy_passed"])
        self.assertEqual(self.archive.latest_evaluation(candidate.candidate_id), result)

    def test_smoke_failure_is_archived_and_skips_regression(self) -> None:
        candidate = self.add_candidate()
        runner = StubSandboxRunner(fail_check="compileall")

        result = self.evaluate(candidate.candidate_id, runner=runner)

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_stage, "smoke")
        self.assertEqual(runner.calls, [("compileall",)])
        self.assertEqual(len(self.archive.list_evaluations(candidate.candidate_id)), 1)

    def test_regression_failure_is_archived(self) -> None:
        candidate = self.add_candidate()
        runner = StubSandboxRunner(fail_check="unittest")

        result = self.evaluate(candidate.candidate_id, runner=runner)

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_stage, "regression")
        self.assertEqual(runner.calls, [("compileall",), ("compileall", "unittest")])

    def test_source_drift_prevents_candidate_execution(self) -> None:
        candidate = self.add_candidate()
        (self.project / "feature.py").write_text("VALUE = 43\n", encoding="utf-8")
        runner = StubSandboxRunner()

        result = self.evaluate(candidate.candidate_id, runner=runner)

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_stage, "source_integrity")
        self.assertEqual(runner.calls, [])

    def test_missing_benchmark_metrics_prevents_pass(self) -> None:
        candidate = self.add_candidate()

        result = self.evaluate(candidate.candidate_id, metrics=None)

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_stage, "benchmark_missing")

    def test_safety_regression_blocks_candidate_even_when_benchmarks_pass(self) -> None:
        candidate = self.add_candidate()
        unsafe = {
            "security_policy_passed": True,
            "policy_regressions": 1,
            "checks": {"workspace_boundary": True},
        }

        result = self.evaluate(candidate.candidate_id, safety=unsafe)

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_stage, "safety")

    def test_invalid_metrics_are_rejected(self) -> None:
        candidate = self.add_candidate()
        invalid = dict(METRICS_A, task_success_rate=1.5)
        with self.assertRaisesRegex(ArchiveError, "between 0 and 1"):
            self.evaluate(candidate.candidate_id, metrics=invalid)

    def test_failed_candidate_cannot_be_retained(self) -> None:
        candidate = self.add_candidate()
        self.evaluate(candidate.candidate_id, runner=StubSandboxRunner("unittest"))
        with self.assertRaisesRegex(ArchiveError, "passing latest evaluation"):
            self.archive.record_decision(
                candidate.candidate_id, decision="retain", reason="Attempted retention."
            )

    def test_promotion_requires_human_approval_and_is_only_an_archive_decision(self) -> None:
        candidate = self.add_candidate()
        self.evaluate(candidate.candidate_id)
        source_before = (self.project / "feature.py").read_text(encoding="utf-8")

        with self.assertRaisesRegex(ArchiveError, "explicit human approval"):
            self.archive.record_decision(
                candidate.candidate_id, decision="promote", reason="Promote candidate."
            )
        decision = self.archive.record_decision(
            candidate.candidate_id,
            decision="promote",
            reason="Reviewed and approved by the user.",
            human_approved=True,
        )

        self.assertTrue(decision.human_approved)
        self.assertEqual((self.project / "feature.py").read_text(encoding="utf-8"), source_before)
        self.assertEqual(len(self.archive.list_decisions(candidate.candidate_id)), 1)

    def test_decisions_are_appended_instead_of_overwriting_history(self) -> None:
        candidate = self.add_candidate()
        self.evaluate(candidate.candidate_id)
        self.archive.record_decision(
            candidate.candidate_id, decision="retain", reason="Initially retained."
        )
        self.archive.record_decision(
            candidate.candidate_id, decision="reject", reason="Later review rejected it."
        )

        decisions = self.archive.list_decisions(candidate.candidate_id)
        self.assertEqual([item.decision for item in decisions], ["retain", "reject"])
        self.assertEqual(self.archive.latest_decision(candidate.candidate_id).decision, "reject")

    def test_parent_selection_uses_pareto_eligible_retained_candidates(self) -> None:
        candidates = [
            self.add_candidate(branch="feature/pareto-a"),
            self.add_candidate(branch="feature/pareto-b"),
            self.add_candidate(branch="feature/dominated"),
        ]
        metrics = [
            METRICS_A,
            {
                "task_success_rate": 0.95,
                "tool_routing_accuracy": 0.85,
                "schema_validity_rate": 0.90,
                "median_latency_ms": 80,
            },
            {
                "task_success_rate": 0.70,
                "tool_routing_accuracy": 0.70,
                "schema_validity_rate": 0.70,
                "median_latency_ms": 180,
            },
        ]
        for candidate, values in zip(candidates, metrics):
            self.evaluate(candidate.candidate_id, metrics=values)
            self.archive.record_decision(
                candidate.candidate_id, decision="retain", reason="Passed evaluation."
            )

        # Add an untested child to the first lineage. It increases that parent's
        # exploration count but cannot itself enter the eligible parent pool.
        self.add_candidate(branch="feature/pareto-a-child", parent=candidates[0].candidate_id)

        selected = self.archive.select_parent(seed=17)
        self.assertIn(selected.candidate_id, {candidates[0].candidate_id, candidates[1].candidate_id})
        self.assertNotEqual(selected.candidate_id, candidates[2].candidate_id)

    def test_real_sandbox_controller_completes_a_passing_evaluation(self) -> None:
        candidate = self.add_candidate()
        controller = CandidateEvaluationController(self.archive, sandbox_runner=SandboxRunner())

        result = controller.evaluate(
            candidate.candidate_id,
            self.project,
            suite_version=SUITE,
            benchmark_metrics=METRICS_A,
            safety_results=SAFETY_PASS,
            timeout_seconds=30,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.smoke_checks[0].name, "compileall")
        self.assertEqual([item.name for item in result.regression_checks], ["compileall", "unittest"])

    def test_capture_stream_closes_pipe_after_reading(self) -> None:
        stream = io.BytesIO(b"test output")

        self.assertEqual(_capture_stream(stream), "test output")
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()

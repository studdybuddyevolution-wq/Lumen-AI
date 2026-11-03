"""Append-only candidate archive and staged evaluation for Lumen.

The archive stores metadata and verification summaries; it does not create Git
worktrees, generate code, install features, or activate candidates. Candidate
source remains in the separately managed branch/worktree identified by its
commit and branch metadata.
"""
from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from backtalk.selfdev.sandbox import (
    SandboxReceipt,
    SandboxRunner,
    project_tree_digest,
)

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_METRIC_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUIRED_METRICS = frozenset({
    "task_success_rate",
    "tool_routing_accuracy",
    "schema_validity_rate",
    "median_latency_ms",
})
_RATE_METRICS = frozenset({
    "task_success_rate",
    "tool_routing_accuracy",
    "schema_validity_rate",
})
_MAX_RECORD_BYTES = 1_000_000


class ArchiveError(ValueError):
    """Raised when archive data or an archive operation is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ArchiveError(f"Changed-file path must be a safe project-relative path: {value!r}")
    return path.as_posix()


def _normalise_metrics(metrics: Mapping[str, Any] | None) -> dict[str, float] | None:
    if metrics is None:
        return None
    if not isinstance(metrics, Mapping):
        raise ArchiveError("benchmark_metrics must be a mapping of metric names to numbers.")

    result: dict[str, float] = {}
    for name, value in metrics.items():
        if not isinstance(name, str) or not _METRIC_PATTERN.fullmatch(name):
            raise ArchiveError(f"Invalid benchmark metric name: {name!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ArchiveError(f"Benchmark metric {name!r} must be numeric.")
        number = float(value)
        if not math.isfinite(number):
            raise ArchiveError(f"Benchmark metric {name!r} must be finite.")
        if name in _RATE_METRICS and not 0.0 <= number <= 1.0:
            raise ArchiveError(f"Benchmark metric {name!r} must be between 0 and 1.")
        if name == "median_latency_ms" and number < 0:
            raise ArchiveError("median_latency_ms cannot be negative.")
        result[name] = number

    missing = _REQUIRED_METRICS.difference(result)
    if missing:
        raise ArchiveError(
            "Benchmark metrics are missing required dimensions: "
            + ", ".join(sorted(missing))
        )
    return result


def _normalise_safety_results(
    safety_results: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if safety_results is None:
        return None
    if not isinstance(safety_results, Mapping):
        raise ArchiveError("safety_results must be a mapping.")

    policy_passed = safety_results.get("security_policy_passed")
    regressions = safety_results.get("policy_regressions")
    if not isinstance(policy_passed, bool):
        raise ArchiveError("safety_results.security_policy_passed must be a boolean.")
    if isinstance(regressions, bool) or not isinstance(regressions, int) or regressions < 0:
        raise ArchiveError("safety_results.policy_regressions must be a non-negative integer.")

    raw_checks = safety_results.get("checks", {})
    if not isinstance(raw_checks, Mapping) or len(raw_checks) > 100:
        raise ArchiveError("safety_results.checks must be a mapping with at most 100 entries.")
    checks: dict[str, bool] = {}
    for name, passed in raw_checks.items():
        if (
            not isinstance(name, str)
            or not _METRIC_PATTERN.fullmatch(name)
            or not isinstance(passed, bool)
        ):
            raise ArchiveError("Safety check names must be identifiers and outcomes must be booleans.")
        checks[name] = passed

    raw_notes = safety_results.get("notes", [])
    if (
        not isinstance(raw_notes, list)
        or len(raw_notes) > 20
        or any(not isinstance(note, str) or len(note) > 500 for note in raw_notes)
    ):
        raise ArchiveError("safety_results.notes must contain at most 20 strings of 500 characters.")

    return {
        "security_policy_passed": policy_passed,
        "policy_regressions": regressions,
        "checks": checks,
        "notes": list(raw_notes),
    }


def _safety_passed(safety: Mapping[str, Any] | None) -> bool:
    return bool(
        safety
        and safety.get("security_policy_passed") is True
        and safety.get("policy_regressions") == 0
        and all(safety.get("checks", {}).values())
    )


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_id: str
    parent_candidate_id: str | None
    source_branch: str
    source_commit: str | None
    source_sha256: str
    changed_files: tuple[str, ...]
    improvement_summary: str
    suite_version: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["changed_files"] = list(self.changed_files)
        return result


@dataclass(frozen=True, slots=True)
class CheckSummary:
    name: str
    passed: bool
    exit_code: int
    timed_out: bool
    duration_ms: int

    @classmethod
    def from_receipt(cls, receipt: SandboxReceipt | None) -> tuple["CheckSummary", ...]:
        if receipt is None:
            return ()
        return tuple(
            cls(
                name=item.name,
                passed=item.passed,
                exit_code=item.exit_code,
                timed_out=item.timed_out,
                duration_ms=item.duration_ms,
            )
            for item in receipt.checks
        )


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    evaluation_id: str
    candidate_id: str
    source_sha256: str
    suite_version: str
    smoke_receipt_id: str | None
    smoke_snapshot_sha256: str | None
    smoke_checks: tuple[CheckSummary, ...]
    regression_receipt_id: str | None
    regression_snapshot_sha256: str | None
    regression_checks: tuple[CheckSummary, ...]
    benchmark_metrics: dict[str, float] | None
    safety_results: dict[str, Any] | None
    passed: bool
    failure_stage: str | None
    environment: dict[str, str]
    started_at: str
    completed_at: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    decision_id: str
    candidate_id: str
    decision: str
    reason: str
    human_approved: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateArchive:
    """Store immutable candidate, evaluation, and decision records.

    Files are append-only through this API. The archive is local metadata, not
    cryptographically tamper-proof storage; filesystem owners can still alter
    or delete files outside this class.
    """

    def __init__(self, archive_dir: str | Path) -> None:
        self.root = Path(archive_dir).expanduser().resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in ("candidates", "evaluations", "decisions"):
                folder = self.root / name
                folder.mkdir(exist_ok=True)
                if folder.is_symlink() or folder.resolve().parent != self.root:
                    raise ArchiveError(f"Archive {name} directory must not be a symlink.")
        except OSError as exc:
            raise ArchiveError(f"Could not initialise candidate archive: {exc}") from exc
        self._lock = threading.RLock()

    def _folder(self, kind: str) -> Path:
        folder = self.root / kind
        if folder.is_symlink() or folder.resolve().parent != self.root:
            raise ArchiveError(f"Archive {kind} directory changed or is unsafe.")
        return folder

    def _write_immutable(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> None:
        if not _ID_PATTERN.fullmatch(record_id):
            raise ArchiveError("Invalid archive record ID.")
        folder = self._folder(kind)
        target = folder / f"{record_id}.json"
        temporary = folder / f".{record_id}.{uuid.uuid4().hex}.tmp"
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise ArchiveError("Archive record already exists; records are immutable.")
            # Each ID is freshly generated; the temporary file keeps readers
            # from observing a partially written JSON record.
            os.rename(temporary, target)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError(f"Could not append {kind} record: {exc}") from exc

    def _read_records(self, kind: str, record_type: type) -> list[Any]:
        records: list[Any] = []
        for path in sorted(self._folder(kind).glob("*.json")):
            if path.is_symlink() or path.stat().st_size > _MAX_RECORD_BYTES:
                raise ArchiveError(f"Archive record is unsafe or too large: {path.name}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("record root must be an object")
                if record_type is CandidateRecord:
                    payload["changed_files"] = tuple(payload.get("changed_files", ()))
                if record_type is CandidateEvaluation:
                    payload["smoke_checks"] = tuple(
                        CheckSummary(**item) for item in payload.get("smoke_checks", ())
                    )
                    payload["regression_checks"] = tuple(
                        CheckSummary(**item) for item in payload.get("regression_checks", ())
                    )
                records.append(record_type(**payload))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                raise ArchiveError(f"Could not read archive record {path.name}: {exc}") from exc
        return records

    def add_candidate(
        self,
        source_dir: str | Path,
        *,
        source_branch: str,
        improvement_summary: str,
        suite_version: str,
        parent_candidate_id: str | None = None,
        source_commit: str | None = None,
        changed_files: tuple[str, ...] | list[str] = (),
    ) -> CandidateRecord:
        """Append a candidate manifest bound to the current filtered tree digest."""
        source_root = Path(source_dir).expanduser().resolve()
        if not source_root.is_dir():
            raise ArchiveError(f"Candidate source directory does not exist: {source_root}")
        try:
            self.root.relative_to(source_root)
        except ValueError:
            pass
        else:
            raise ArchiveError("Candidate archive must be outside the candidate source tree.")

        if (
            not isinstance(source_branch, str)
            or not _BRANCH_PATTERN.fullmatch(source_branch)
            or ".." in source_branch
            or "//" in source_branch
            or source_branch.endswith(("/", ".lock"))
        ):
            raise ArchiveError("source_branch is not a safe Git branch name.")
        if source_commit is not None and (
            not isinstance(source_commit, str) or not _COMMIT_PATTERN.fullmatch(source_commit)
        ):
            raise ArchiveError("source_commit must be a 7-64 character hexadecimal commit ID.")
        if (
            not isinstance(improvement_summary, str)
            or not improvement_summary.strip()
            or len(improvement_summary.strip()) > 1000
        ):
            raise ArchiveError("improvement_summary must contain 1-1000 characters.")
        if not isinstance(suite_version, str) or not suite_version.strip() or len(suite_version) > 128:
            raise ArchiveError("suite_version must contain 1-128 characters.")

        safe_files = tuple(_safe_relative_path(item) for item in changed_files)
        if len(safe_files) > 500 or len(set(safe_files)) != len(safe_files):
            raise ArchiveError("changed_files must contain at most 500 unique project-relative paths.")

        if parent_candidate_id is not None:
            parent = self.get_candidate(parent_candidate_id)
            parent_decision = self.latest_decision(parent.candidate_id)
            parent_evaluation = self.latest_evaluation(parent.candidate_id)
            if (
                parent_decision is None
                or parent_decision.decision not in {"retain", "promote"}
                or parent_evaluation is None
                or not parent_evaluation.passed
            ):
                raise ArchiveError("A child must reference a previously retained, passing parent candidate.")

        try:
            source_digest = project_tree_digest(source_root)
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"Could not digest candidate source: {exc}") from exc

        record = CandidateRecord(
            candidate_id=uuid.uuid4().hex,
            parent_candidate_id=parent_candidate_id,
            source_branch=source_branch,
            source_commit=source_commit.lower() if source_commit else None,
            source_sha256=source_digest,
            changed_files=safe_files,
            improvement_summary=improvement_summary.strip(),
            suite_version=suite_version.strip(),
            created_at=_now(),
        )
        with self._lock:
            self._write_immutable("candidates", record.candidate_id, record.to_dict())
        return record

    def get_candidate(self, candidate_id: str) -> CandidateRecord:
        if not isinstance(candidate_id, str) or not _ID_PATTERN.fullmatch(candidate_id):
            raise ArchiveError("Invalid candidate ID.")
        path = self._folder("candidates") / f"{candidate_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ArchiveError(f"Candidate not found: {candidate_id}")
        try:
            if path.stat().st_size > _MAX_RECORD_BYTES:
                raise ArchiveError("Candidate record exceeds the archive size limit.")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["changed_files"] = tuple(payload.get("changed_files", ()))
            return CandidateRecord(**payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError(f"Could not read candidate {candidate_id}: {exc}") from exc

    def list_candidates(self) -> list[CandidateRecord]:
        return self._read_records("candidates", CandidateRecord)

    def list_evaluations(self, candidate_id: str | None = None) -> list[CandidateEvaluation]:
        records = self._read_records("evaluations", CandidateEvaluation)
        if candidate_id is not None:
            records = [item for item in records if item.candidate_id == candidate_id]
        return sorted(records, key=lambda item: (item.completed_at, item.evaluation_id))

    def list_decisions(self, candidate_id: str | None = None) -> list[CandidateDecision]:
        records = self._read_records("decisions", CandidateDecision)
        if candidate_id is not None:
            records = [item for item in records if item.candidate_id == candidate_id]
        return sorted(records, key=lambda item: (item.created_at, item.decision_id))

    def latest_evaluation(self, candidate_id: str) -> CandidateEvaluation | None:
        records = self.list_evaluations(candidate_id)
        return records[-1] if records else None

    def latest_decision(self, candidate_id: str) -> CandidateDecision | None:
        records = self.list_decisions(candidate_id)
        return records[-1] if records else None

    def _append_evaluation(self, record: CandidateEvaluation) -> None:
        candidate = self.get_candidate(record.candidate_id)
        if record.source_sha256 != candidate.source_sha256:
            raise ArchiveError("Evaluation digest does not match the candidate manifest.")
        if record.suite_version != candidate.suite_version:
            raise ArchiveError("Evaluation suite version does not match the candidate manifest.")
        with self._lock:
            self._write_immutable("evaluations", record.evaluation_id, record.to_dict())

    def record_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: str,
        human_approved: bool = False,
    ) -> CandidateDecision:
        """Append a review decision; promotion requires explicit human approval.

        A promotion decision is only archive metadata. It never installs or
        activates the candidate in Lumen.
        """
        candidate = self.get_candidate(candidate_id)
        if decision not in {"retain", "reject", "promote"}:
            raise ArchiveError("decision must be retain, reject, or promote.")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ArchiveError("decision reason must contain 1-1000 characters.")
        if not isinstance(human_approved, bool):
            raise ArchiveError("human_approved must be a boolean.")

        latest = self.latest_evaluation(candidate.candidate_id)
        if decision in {"retain", "promote"} and (latest is None or not latest.passed):
            raise ArchiveError("Only a candidate with a passing latest evaluation may be retained or promoted.")
        if decision == "promote" and not human_approved:
            raise ArchiveError("Promotion requires explicit human approval.")

        record = CandidateDecision(
            decision_id=uuid.uuid4().hex,
            candidate_id=candidate.candidate_id,
            decision=decision,
            reason=reason.strip(),
            human_approved=human_approved,
            created_at=_now(),
        )
        with self._lock:
            self._write_immutable("decisions", record.decision_id, record.to_dict())
        return record

    def select_parent(self, *, seed: int = 0) -> CandidateRecord:
        """Choose among retained Pareto-front candidates, favouring less-used lineages.

        Capability dimensions remain separate: task success, tool-routing
        accuracy, schema validity, and latency. Safety is a hard eligibility
        gate and is never traded for a higher capability score.
        """
        candidates = self.list_candidates()
        eligible: list[tuple[CandidateRecord, CandidateEvaluation]] = []
        for candidate in candidates:
            decision = self.latest_decision(candidate.candidate_id)
            evaluation = self.latest_evaluation(candidate.candidate_id)
            if (
                decision is not None
                and decision.decision in {"retain", "promote"}
                and evaluation is not None
                and evaluation.passed
                and _safety_passed(evaluation.safety_results)
                and evaluation.benchmark_metrics is not None
            ):
                eligible.append((candidate, evaluation))

        if not eligible:
            raise ArchiveError("No retained candidate has a passing evaluation and safety result.")

        def vector(item: tuple[CandidateRecord, CandidateEvaluation]) -> tuple[float, ...]:
            metrics = item[1].benchmark_metrics or {}
            return (
                metrics["task_success_rate"],
                metrics["tool_routing_accuracy"],
                metrics["schema_validity_rate"],
                -metrics["median_latency_ms"],
            )

        front: list[tuple[CandidateRecord, CandidateEvaluation]] = []
        for item in eligible:
            values = vector(item)
            dominated = False
            for other in eligible:
                if other[0].candidate_id == item[0].candidate_id:
                    continue
                other_values = vector(other)
                if all(a >= b for a, b in zip(other_values, values)) and any(
                    a > b for a, b in zip(other_values, values)
                ):
                    dominated = True
                    break
            if not dominated:
                front.append(item)

        child_counts: dict[str, int] = {candidate.candidate_id: 0 for candidate, _ in eligible}
        for candidate in candidates:
            if candidate.parent_candidate_id in child_counts:
                child_counts[candidate.parent_candidate_id] += 1

        front.sort(key=lambda item: item[0].candidate_id)
        weights = [1.0 / (1 + child_counts[item[0].candidate_id]) for item in front]
        return random.Random(seed).choices(front, weights=weights, k=1)[0][0]


class CandidateEvaluationController:
    """Run staged checks and append an evidence-bound evaluation record.

    The benchmark metrics and policy checks are supplied by a trusted evaluator;
    this class does not invent capability scores or treat test success alone as
    proof of safety.
    """

    def __init__(
        self,
        archive: CandidateArchive,
        *,
        sandbox_runner: SandboxRunner | None = None,
    ) -> None:
        self.archive = archive
        self.sandbox_runner = sandbox_runner or SandboxRunner()

    def evaluate(
        self,
        candidate_id: str,
        source_dir: str | Path,
        *,
        suite_version: str,
        benchmark_metrics: Mapping[str, Any] | None = None,
        safety_results: Mapping[str, Any] | None = None,
        timeout_seconds: int = 180,
    ) -> CandidateEvaluation:
        candidate = self.archive.get_candidate(candidate_id)
        if suite_version != candidate.suite_version:
            raise ArchiveError("Requested evaluation suite does not match the candidate manifest.")
        metrics = _normalise_metrics(benchmark_metrics)
        safety = _normalise_safety_results(safety_results)
        source_root = Path(source_dir).expanduser().resolve()
        started_at = _now()
        started_clock = time.perf_counter()
        smoke: SandboxReceipt | None = None
        regression: SandboxReceipt | None = None
        integrity_ok = False

        try:
            integrity_ok = project_tree_digest(source_root) == candidate.source_sha256
        except (OSError, ValueError):
            integrity_ok = False

        if integrity_ok:
            smoke = self.sandbox_runner.run_checks(
                source_root,
                checks=("compileall",),
                timeout_seconds=timeout_seconds,
            )
            if smoke.passed and smoke.source_sha256 == candidate.source_sha256:
                regression = self.sandbox_runner.run_checks(
                    source_root,
                    checks=("compileall", "unittest"),
                    timeout_seconds=timeout_seconds,
                )

        try:
            final_digest = project_tree_digest(source_root)
        except (OSError, ValueError):
            final_digest = ""
        if final_digest != candidate.source_sha256:
            integrity_ok = False

        if not integrity_ok:
            failure_stage = "source_integrity"
        elif smoke is None or not smoke.passed or smoke.source_sha256 != candidate.source_sha256:
            failure_stage = "smoke"
        elif (
            regression is None
            or not regression.passed
            or regression.source_sha256 != candidate.source_sha256
        ):
            failure_stage = "regression"
        elif metrics is None:
            failure_stage = "benchmark_missing"
        elif safety is None:
            failure_stage = "safety_missing"
        elif not _safety_passed(safety):
            failure_stage = "safety"
        else:
            failure_stage = None

        record = CandidateEvaluation(
            evaluation_id=uuid.uuid4().hex,
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            suite_version=candidate.suite_version,
            smoke_receipt_id=smoke.receipt_id if smoke else None,
            smoke_snapshot_sha256=smoke.snapshot_sha256 if smoke else None,
            smoke_checks=CheckSummary.from_receipt(smoke),
            regression_receipt_id=regression.receipt_id if regression else None,
            regression_snapshot_sha256=regression.snapshot_sha256 if regression else None,
            regression_checks=CheckSummary.from_receipt(regression),
            benchmark_metrics=metrics,
            safety_results=safety,
            passed=failure_stage is None,
            failure_stage=failure_stage,
            environment={
                "python": platform.python_version(),
                "platform": platform.system(),
                "platform_release": platform.release(),
            },
            started_at=started_at,
            completed_at=_now(),
            duration_ms=round((time.perf_counter() - started_clock) * 1000),
        )
        self.archive._append_evaluation(record)
        return record

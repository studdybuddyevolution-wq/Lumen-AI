"""Disposable, copy-based test runner for proposed Lumen changes.

This protects the checked-out project from test-side file edits by executing
checks against a temporary snapshot in a separate Python process. It is NOT
an OS security boundary: test code can still access files and network resources
available to the current Windows account. Do not use it as a hostile-code
sandbox. A restricted Windows Sandbox or container backend is a future hardening
requirement for genuinely untrusted code.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_ALLOWED_CHECKS = frozenset({"compileall", "unittest", "pytest"})
_IGNORED_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", "node_modules",
    "dist", "build",
})
_MAX_FILES = 10_000
_MAX_BYTES = 500 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 600
_MAX_OUTPUT_CHARS = 20_000

_BOOTSTRAP = (
    "import os, runpy, sys; "
    "workspace, module = sys.argv[1], sys.argv[2]; "
    "sys.path.insert(0, workspace); "
    "os.chdir(workspace); "
    "sys.argv = [module, *sys.argv[3:]]; "
    "runpy.run_module(module, run_name='__main__')"
)


class SandboxError(ValueError):
    """Raised when a sandbox request is invalid or cannot be prepared."""


@dataclass(frozen=True, slots=True)
class SandboxCheckResult:
    name: str
    passed: bool
    exit_code: int
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class SandboxReceipt:
    receipt_id: str
    source_sha256: str
    snapshot_sha256: str
    subject_path: str | None
    subject_sha256: str | None
    passed: bool
    checks: tuple[SandboxCheckResult, ...]
    started_at: str
    finished_at: str
    duration_ms: int
    isolation: str
    security_boundary: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        """Return a JSON-compatible receipt suitable for audit or sidecars."""
        result = asdict(self)
        result["checks"] = [asdict(check) for check in self.checks]
        return result


def _safe_environment(temp_root: Path) -> dict[str, str]:
    """Keep platform essentials but remove common credential-bearing variables."""
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME"}
        and not any(marker in key.upper() for marker in secret_markers)
    }
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LUMEN_SANDBOX"] = "1"
    env["LUMEN_SANDBOX_ROOT"] = str(temp_root)
    env["TEMP"] = str(temp_root)
    env["TMP"] = str(temp_root)
    return env


def _included_file(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or (
        name.startswith(".env.") and name not in {".env.example", ".env.sample"}
    ):
        return False
    if name in {"backtalk.json", "secrets.json", "credentials.json"}:
        return False
    if name.endswith((".approved", ".verified")):
        return False
    return True


def _walk_files(root: Path) -> Iterable[tuple[Path, Path]]:
    """Yield (source, relative) files without following symlinks."""
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            child = current_path / dirname
            if dirname.lower() in _IGNORED_DIRS or child.is_symlink():
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            source = current_path / filename
            if source.is_symlink() or not _included_file(source):
                continue
            yield source, source.relative_to(root)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for source, relative in sorted(_walk_files(root), key=lambda item: item[1].as_posix()):
        digest.update(relative.as_posix().encode("utf-8", errors="replace"))
        digest.update(b"\0")
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_snapshot(source_root: Path, destination: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    for source, relative in _walk_files(source_root):
        stat = source.stat()
        file_count += 1
        total_bytes += stat.st_size
        if file_count > _MAX_FILES:
            raise SandboxError(f"Project snapshot exceeds the {_MAX_FILES} file limit.")
        if total_bytes > _MAX_BYTES:
            raise SandboxError("Project snapshot exceeds the 500 MiB size limit.")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return file_count, total_bytes


def _module_arguments(check: str, workspace: Path) -> tuple[str, list[str]]:
    if check == "compileall":
        return "compileall", ["-q", "."]
    if check == "unittest":
        args = ["discover", "-v"]
        if (workspace / "tests").is_dir():
            args[1:1] = ["-s", "tests"]
        return "unittest", args
    if check == "pytest":
        if not (workspace / "tests").is_dir():
            raise SandboxError("pytest check requested, but the snapshot has no tests directory.")
        return "pytest", ["-q", "tests"]
    raise SandboxError(f"Unsupported sandbox check: {check}")


def _terminate_process(process: subprocess.Popen) -> None:
    """Best-effort termination of a timed-out check and its child processes."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _capture_stream(stream) -> str:
    """Drain a child pipe while retaining only a bounded output prefix."""
    captured = bytearray()
    truncated = False
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        available = _MAX_OUTPUT_CHARS - len(captured)
        if available > 0:
            captured.extend(chunk[:available])
        if len(chunk) > available:
            truncated = True
    text = captured.decode("utf-8", errors="replace")
    if truncated:
        text += "\n[output truncated]"
    return text


def _run_check(
    check: str,
    workspace: Path,
    temp_root: Path,
    timeout_seconds: int,
) -> SandboxCheckResult:
    module, args = _module_arguments(check, workspace)
    command = [sys.executable, "-I", "-c", _BOOTSTRAP, str(workspace), module, *args]
    env = _safe_environment(temp_root)
    started = time.perf_counter()
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        output: dict[str, str] = {}
        readers = [
            threading.Thread(
                target=lambda: output.setdefault("stdout", _capture_stream(process.stdout)),
                name="lumen-sandbox-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=lambda: output.setdefault("stderr", _capture_stream(process.stderr)),
                name="lumen-sandbox-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=3)
        stdout = output.get("stdout", "")
        stderr = output.get("stderr", "")
        if timed_out:
            stderr = (stderr + "\nSandbox check timed out.").strip()
        exit_code = process.returncode if process.returncode is not None else -1
    except OSError as exc:
        stdout, stderr, exit_code = "", f"Could not start check: {exc}", -1

    elapsed = round((time.perf_counter() - started) * 1000)
    return SandboxCheckResult(
        name=check,
        passed=exit_code == 0 and not timed_out,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=elapsed,
        stdout=stdout[-_MAX_OUTPUT_CHARS:],
        stderr=stderr[-_MAX_OUTPUT_CHARS:],
    )

class SandboxRunner:
    """Run fixed Python verification checks against a disposable source snapshot."""

    def run_checks(
        self,
        source_dir: str | Path,
        *,
        checks: tuple[str, ...] = ("compileall", "unittest"),
        timeout_seconds: int = 180,
        subject_path: str | Path | None = None,
    ) -> SandboxReceipt:
        """Copy a project, run allowed checks, and return a digest-bound receipt.

        subject_path, when supplied, must identify a file inside source_dir.
        Only the snapshot is executed; the original working tree is not used as
        the test working directory.
        """
        root = Path(source_dir).expanduser().resolve()
        if not root.is_dir():
            raise SandboxError(f"Source directory does not exist: {root}")
        if not checks or any(check not in _ALLOWED_CHECKS for check in checks):
            raise SandboxError(
                "Checks must be a non-empty subset of: compileall, unittest, pytest."
            )
        if len(set(checks)) != len(checks):
            raise SandboxError("Duplicate sandbox checks are not allowed.")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise SandboxError(f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}.")

        subject_relative: str | None = None
        subject_digest: str | None = None
        if subject_path is not None:
            subject = Path(subject_path)
            candidate = (root / subject).resolve() if not subject.is_absolute() else subject.resolve()
            try:
                subject_relative = candidate.relative_to(root).as_posix()
            except ValueError as exc:
                raise SandboxError("subject_path must be inside source_dir.") from exc
            if not candidate.is_file() or candidate.is_symlink():
                raise SandboxError("subject_path must be an existing regular file.")
            subject_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()

        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started_clock = time.perf_counter()
        source_digest = _tree_digest(root)
        check_results: list[SandboxCheckResult] = []
        snapshot_digest = ""
        warnings = (
            "Workspace-copy and subprocess isolation only; this is not a security sandbox.",
            "Tests can still access files and network resources permitted to the current Windows account.",
            "Do not run hostile third-party code here; use a restricted container or Windows Sandbox backend for that.",
        )

        with tempfile.TemporaryDirectory(prefix="lumen-selfdev-") as temporary:
            temp_root = Path(temporary)
            workspace = temp_root / "workspace"
            workspace.mkdir()
            _copy_snapshot(root, workspace)
            snapshot_digest = _tree_digest(workspace)

            for check in checks:
                result = _run_check(check, workspace, temp_root, timeout_seconds)
                check_results.append(result)
                if not result.passed:
                    break

        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        original_unchanged = _tree_digest(root) == source_digest
        all_passed = bool(check_results) and len(check_results) == len(checks) and all(
            result.passed for result in check_results
        )
        if not original_unchanged:
            all_passed = False
            warnings += ("The original source tree changed during verification; receipt rejected.",)

        return SandboxReceipt(
            receipt_id=uuid.uuid4().hex,
            source_sha256=source_digest,
            snapshot_sha256=snapshot_digest,
            subject_path=subject_relative,
            subject_sha256=subject_digest,
            passed=all_passed,
            checks=tuple(check_results),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=round((time.perf_counter() - started_clock) * 1000),
            isolation="temporary-copy-subprocess",
            security_boundary=False,
            warnings=warnings,
        )

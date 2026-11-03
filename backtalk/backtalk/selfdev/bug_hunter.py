"""Bounded, privacy-conscious runtime bug capture for Lumen.

BugHunter records sanitised exception summaries and source locations in memory.
It never stores locals, function arguments, conversation text, or raw tracebacks,
and it never edits or repairs source files.
"""
from __future__ import annotations

import ast
import hashlib
import re
import threading
import traceback
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_FRAME_RE = re.compile(
    r'^\s*File "(?P<path>.+?)", line (?P<line>\d+), in (?P<function>.+?)\s*$'
)
_EXCEPTION_RE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(?P<message>.*)$"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|
       password|passwd|secret|client[_-]?secret|authorization)
    \b\s*[:=]\s*
    (?:["']?)[^\s,;}\]]+
    """
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
)
_WINDOWS_USER_PATH_RE = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+")
_LONG_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b")
_HEX_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{8,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_MAX_TRACEBACK_CHARS = 32_000
_MAX_FRAME_COUNT = 20
_MAX_SOURCE_BYTES = 512 * 1024
_MAX_CONTEXT_LINES = 40
_MAX_MESSAGE_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact_text(value: str, *, limit: int = _MAX_MESSAGE_CHARS) -> str:
    """Remove common inline credentials and bound untrusted diagnostic text."""
    text = str(value or "")
    if _PRIVATE_KEY_BLOCK_RE.search(text):
        text = _PRIVATE_KEY_BLOCK_RE.sub("[private-key-redacted]", text)
    text = _WINDOWS_USER_PATH_RE.sub("%USERPROFILE%", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: match.group(0).split("=")[0].split(":")[0] + "=[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _LONG_TOKEN_RE.sub("[token-redacted]", text)
    text = _HEX_ADDRESS_RE.sub("0x[redacted-address]", text)
    text = _UUID_RE.sub("[uuid]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…[truncated]"
    return text


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "runtime"))
    return cleaned[:80] or "runtime"


def _parse_frames(text: str) -> list[tuple[str, int, str]]:
    frames: list[tuple[str, int, str]] = []
    for line in text.splitlines():
        match = _FRAME_RE.match(line)
        if not match:
            continue
        try:
            number = int(match.group("line"))
        except ValueError:
            continue
        if number < 1:
            continue
        frames.append((
            match.group("path"),
            number,
            redact_text(match.group("function"), limit=120),
        ))
    return frames[-_MAX_FRAME_COUNT:]


def _resolve_project_file(project_root: Path, filename: str) -> tuple[Path, str] | None:
    try:
        raw = Path(filename)
        if not raw.is_absolute():
            raw = project_root / raw
        resolved = raw.resolve(strict=True)
        relative = resolved.relative_to(project_root)
        cursor = project_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        if not resolved.is_file() or resolved.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        return resolved, relative.as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _node_name(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    return ""


def _find_ast_symbol(tree: ast.AST, lineno: int) -> str | None:
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name = _node_name(node)
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if name and isinstance(start, int) and isinstance(end, int):
            if start <= lineno <= end:
                matches.append((end - start, name))
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


@dataclass(frozen=True, slots=True)
class BugFrame:
    file: str
    line: int
    function: str
    source_line: str


@dataclass(frozen=True, slots=True)
class BugReport:
    fingerprint: str
    exception_type: str
    message: str
    component: str
    primary_file: str | None
    primary_line: int | None
    primary_function: str | None
    ast_symbol: str | None
    source_context: tuple[str, ...]
    frames: tuple[BugFrame, ...]
    occurrence_count: int
    first_seen: str
    last_seen: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_context"] = list(self.source_context)
        result["frames"] = [asdict(frame) for frame in self.frames]
        return result


class BugHunter:
    """Collect deduplicated, source-aware runtime error reports in memory."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        max_reports: int = 100,
        max_traceback_chars: int = _MAX_TRACEBACK_CHARS,
    ) -> None:
        if not isinstance(max_reports, int) or max_reports < 1:
            raise ValueError("max_reports must be at least 1.")
        if (
            not isinstance(max_traceback_chars, int)
            or not 1024 <= max_traceback_chars <= 1_000_000
        ):
            raise ValueError("max_traceback_chars must be between 1024 and 1000000.")
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("project_root must be an existing directory.")
        self.project_root = root
        self.max_reports = max_reports
        self.max_traceback_chars = max_traceback_chars
        self._reports: OrderedDict[str, BugReport] = OrderedDict()
        self._total_events = 0
        self._lock = threading.RLock()

    def _source_context(
        self, filename: str, lineno: int
    ) -> tuple[str, str, tuple[str, ...]]:
        resolved = _resolve_project_file(self.project_root, filename)
        if resolved is None:
            return "", "", ()
        path, relative = resolved
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
        except (OSError, UnicodeError, ValueError, MemoryError):
            return relative, "", ()

        try:
            tree = ast.parse(source, filename=relative)
            symbol = _find_ast_symbol(tree, lineno) or ""
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            # Syntax errors are useful Bug Hunter inputs too. Preserve the
            # bounded line excerpt even when the file cannot form an AST.
            symbol = ""

        if not lines:
            return relative, symbol, ()

        start = max(1, lineno - 3)
        end = min(len(lines), lineno + 4)
        if end - start + 1 > _MAX_CONTEXT_LINES:
            end = start + _MAX_CONTEXT_LINES - 1
        context = tuple(
            redact_text(lines[index - 1], limit=300)
            for index in range(start, end + 1)
        )
        return relative, symbol, context

    def capture_traceback(
        self,
        traceback_text: str,
        *,
        exception_type: str = "Exception",
        message: str | None = None,
        component: str = "runtime",
    ) -> BugReport:
        """Capture a formatted traceback without retaining the raw text."""
        safe_traceback = str(traceback_text or "")[: self.max_traceback_chars]
        frames_raw = _parse_frames(safe_traceback)
        safe_type = re.sub(r"[^A-Za-z0-9_.]", "", str(exception_type or "Exception"))[:120]
        if not safe_type:
            safe_type = "Exception"

        if message is None:
            summary = ""
            for line in reversed(safe_traceback.splitlines()):
                candidate = line.strip()
                if candidate and not candidate.startswith(("^", "~")):
                    summary = candidate
                    break
            match = _EXCEPTION_RE.match(summary)
            if match:
                safe_type = safe_type if safe_type != "Exception" else match.group("type")[-120:]
                message = match.group("message")
            else:
                message = summary
        safe_message = redact_text(message or "(no exception message)")
        safe_component = _safe_component(component)

        resolved_frames: list[BugFrame] = []
        primary_file: str | None = None
        primary_line: int | None = None
        primary_function: str | None = None
        ast_symbol: str | None = None
        source_context: tuple[str, ...] = ()

        for filename, lineno, function in frames_raw:
            resolved = _resolve_project_file(self.project_root, filename)
            if resolved is None:
                continue
            path, relative = resolved
            try:
                source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                source_line = (
                    redact_text(source_lines[lineno - 1], limit=300)
                    if 0 < lineno <= len(source_lines)
                    else ""
                )
            except (OSError, UnicodeError, MemoryError):
                source_line = ""
            resolved_frames.append(BugFrame(
                file=relative,
                line=lineno,
                function=function,
                source_line=source_line,
            ))

        if resolved_frames:
            primary = resolved_frames[-1]
            primary_file = primary.file
            primary_line = primary.line
            primary_function = primary.function
            context_file, symbol, context = self._source_context(
                str(self.project_root / primary.file), primary.line
            )
            if context_file == primary.file:
                ast_symbol = symbol or None
                source_context = context

        normalised_message = _UUID_RE.sub("[uuid]", safe_message)
        normalised_message = re.sub(r"\b\d+\b", "#", normalised_message).lower()
        fingerprint_source = "\0".join((
            safe_type.lower(),
            normalised_message,
            primary_file or "<external>",
            primary_function or "<unknown>",
        ))
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:32]
        timestamp = _now()

        with self._lock:
            self._total_events += 1
            existing = self._reports.get(fingerprint)
            if existing is not None:
                report = BugReport(
                    fingerprint=fingerprint,
                    exception_type=existing.exception_type,
                    message=existing.message,
                    component=existing.component,
                    primary_file=existing.primary_file,
                    primary_line=existing.primary_line,
                    primary_function=existing.primary_function,
                    ast_symbol=existing.ast_symbol,
                    source_context=existing.source_context,
                    frames=existing.frames,
                    occurrence_count=existing.occurrence_count + 1,
                    first_seen=existing.first_seen,
                    last_seen=timestamp,
                )
                self._reports[fingerprint] = report
                self._reports.move_to_end(fingerprint)
                return report

            report = BugReport(
                fingerprint=fingerprint,
                exception_type=safe_type,
                message=safe_message,
                component=safe_component,
                primary_file=primary_file,
                primary_line=primary_line,
                primary_function=primary_function,
                ast_symbol=ast_symbol,
                source_context=source_context,
                frames=tuple(resolved_frames),
                occurrence_count=1,
                first_seen=timestamp,
                last_seen=timestamp,
            )
            self._reports[fingerprint] = report
            while len(self._reports) > self.max_reports:
                self._reports.popitem(last=False)
            return report

    def capture_exception(
        self,
        exc: BaseException,
        *,
        component: str = "runtime",
    ) -> BugReport:
        """Capture an exception object, formatting only its traceback frames."""
        try:
            rendered = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__, limit=40)
            )
        except Exception:
            rendered = f"{type(exc).__name__}: {exc}"
        return self.capture_traceback(
            rendered,
            exception_type=type(exc).__name__,
            message=str(exc),
            component=component,
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return newest-first report snapshots with no mutable internal state."""
        safe_limit = max(0, min(int(limit), self.max_reports))
        with self._lock:
            reports = list(self._reports.values())[-safe_limit:] if safe_limit else []
            return [report.to_dict() for report in reversed(reports)]

    def summary(self, limit: int = 50) -> dict[str, Any]:
        """Return bounded bug counts and recent sanitised report details."""
        with self._lock:
            reports = self.recent(limit)
            return {
                "events_seen": self._total_events,
                "unique_reports": len(self._reports),
                "recurrent_reports": sum(
                    1 for report in self._reports.values()
                    if report.occurrence_count > 1
                ),
                "reports": reports,
            }

"""Controlled preparation workflow for model-generated Lumen tools.

Skill Forge creates candidate source only in dedicated Git worktrees. Static
validation never imports or executes candidate code. The existing copy/process
SandboxRunner is deliberately not used for generated candidates because it is
not an operating-system security boundary.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from backtalk.selfdev.archive import CandidateArchive, CandidateRecord
from backtalk.selfdev.sandbox import project_tree_digest

_SKILL_ID = re.compile(r"^[0-9a-f]{32}$")
_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_PYTHON_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_MAX_TEXT = 100_000
_MAX_FILES = 8
_MAX_GIT_OUTPUT = 8_000
_MAX_GIT_TIMEOUT = 20
_MAX_LEDGER_BYTES = 1_000_000
_SUITE_VERSION = "skill-forge-static-v1"
_ALLOWED_IMPORTS = frozenset({
    "collections", "dataclasses", "datetime", "decimal", "enum",
    "functools", "itertools", "json", "math", "re", "statistics",
    "string", "typing",
})
_BLOCKED_CALLS = frozenset({
    "__import__", "breakpoint", "compile", "delattr", "eval", "exec",
    "execfile", "getattr", "get_type_hints", "globals", "help", "input", "locals", "open",
    "print", "quit", "exit", "setattr", "vars",
})


class SkillForgeError(RuntimeError):
    """Raised when a Skill Forge operation cannot be completed safely."""


class SkillGenerationError(SkillForgeError):
    """Raised when the local model does not return a valid candidate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise SkillForgeError("Candidate file path must contain 1-240 characters.")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    path = PurePosixPath(normalized)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized)
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in reserved
            for part in parts
        )
        or "\x00" in normalized
    ):
        raise SkillForgeError(f"Unsafe candidate-relative path: {value!r}")
    return path.as_posix()


def _bounded_text(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SkillForgeError(f"{label} must be text.")
    result = value.strip()
    if required and not result:
        raise SkillForgeError(f"{label} cannot be empty.")
    if len(result) > _MAX_TEXT:
        raise SkillForgeError(f"{label} exceeds {_MAX_TEXT} characters.")
    return result


@dataclass(frozen=True, slots=True)
class SkillSpecification:
    """Reviewable description of one proposed pure-Python Lumen tool."""

    name: str
    description: str
    intended_use: str
    entry_point: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    candidate_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    required_permissions: tuple[str, ...] = ()
    filesystem_scope: tuple[str, ...] = ()
    network_access_required: bool = False
    dependencies: tuple[str, ...] = ()
    expected_behavior: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()
    proposed_tests: tuple[str, ...] = ()
    parent_candidate_id: str | None = None
    originating_request: str = ""

    def validate(self) -> None:
        if not isinstance(self.candidate_id, str) or not _SKILL_ID.fullmatch(self.candidate_id):
            raise SkillForgeError("candidate_id must be a 32-character lowercase hexadecimal ID.")
        if not isinstance(self.name, str) or not _SKILL_NAME.fullmatch(self.name):
            raise SkillForgeError("Skill name must be a lowercase Python identifier.")
        _bounded_text(self.description, "description")
        _bounded_text(self.intended_use, "intended_use")
        if not isinstance(self.entry_point, str):
            raise SkillForgeError("entry_point must be text.")
        module, separator, function = self.entry_point.partition(":")
        if (
            not separator
            or module != f"backtalk.features.{self.name}"
            or not _PYTHON_NAME.fullmatch(function)
            or function.startswith("__")
        ):
            raise SkillForgeError(
                "entry_point must use backtalk.features.<skill_name>:<function_name>."
            )
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise SkillForgeError("input_schema must be a JSON Schema object.")
        if not isinstance(self.output_schema, dict):
            raise SkillForgeError("output_schema must be a JSON Schema object.")
        try:
            Draft202012Validator.check_schema(self.input_schema)
            Draft202012Validator.check_schema(self.output_schema)
        except Exception as exc:
            raise SkillForgeError(f"Invalid skill JSON Schema: {exc}") from exc
        for label, values, maximum in (
            ("required_permissions", self.required_permissions, 20),
            ("filesystem_scope", self.filesystem_scope, 20),
            ("dependencies", self.dependencies, 20),
            ("expected_behavior", self.expected_behavior, 30),
            ("failure_modes", self.failure_modes, 30),
            ("proposed_tests", self.proposed_tests, 50),
        ):
            if not isinstance(values, (tuple, list)) or len(values) > maximum:
                raise SkillForgeError(f"{label} must be a list with at most {maximum} items.")
            if any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in values):
                raise SkillForgeError(f"{label} contains an invalid item.")
        for item in self.filesystem_scope:
            _safe_relative_path(item)
        if not isinstance(self.network_access_required, bool):
            raise SkillForgeError("network_access_required must be boolean.")
        if self.parent_candidate_id is not None and (
            not isinstance(self.parent_candidate_id, str)
            or not _SKILL_ID.fullmatch(self.parent_candidate_id)
        ):
            raise SkillForgeError("parent_candidate_id is invalid.")
        _bounded_text(self.originating_request, "originating_request", required=False)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        for name in (
            "required_permissions", "filesystem_scope", "dependencies",
            "expected_behavior", "failure_modes", "proposed_tests",
        ):
            result[name] = list(result[name])
        return result

    @property
    def source_path(self) -> str:
        return f"backtalk/backtalk/features/{self.name}.py"

    @property
    def test_path(self) -> str:
        return f"backtalk/tests/test_skillforge_{self.name}.py"


@dataclass(frozen=True, slots=True)
class GeneratedSkill:
    source: str
    tests: str

    def validate(self) -> None:
        _bounded_text(self.source, "generated source")
        _bounded_text(self.tests, "generated tests")
        if len(self.source) > _MAX_TEXT or len(self.tests) > _MAX_TEXT:
            raise SkillGenerationError("Generated source or tests exceed the size limit.")


class SkillGenerator(Protocol):
    name: str

    def generate(self, specification: SkillSpecification) -> GeneratedSkill:
        """Generate source and tests from only the supplied specification."""


class OllamaSkillGenerator:
    """Local-only Ollama generator; it never receives chat or vault history."""

    name = "ollama-local"

    def __init__(
        self,
        model: str = "qwen3.5:4b",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 90,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SkillForgeError("Ollama generation must use an HTTP loopback URL.")
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise SkillForgeError("A valid local Ollama model name is required.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 5 <= timeout_seconds <= 180:
            raise SkillForgeError("timeout_seconds must be between 5 and 180.")
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate(self, specification: SkillSpecification) -> GeneratedSkill:
        payload = specification.to_dict()
        system_prompt = (
            "Generate one proposed Lumen Python tool. Return only a JSON object with "
            "exactly two string fields: source and tests. The source must define the "
            "specified synchronous function and register it with "
            "'from backtalk.registry import tool' and @tool(name=..., description=..., "
            "params_schema=..., mutating=False), using literal values that exactly "
            "match the supplied specification. Match every JSON Schema property to "
            "a function parameter, with defaults only for optional properties. Use "
            "pure computation only. Do not access "
            "files, environment variables, credentials, the network, subprocesses, "
            "the operating system, the Obsidian vault, chat history, or package "
            "installation. Do not add dependencies. No module-level side effects. "
            "The tests must be standard-library unittest tests. These files will "
            "receive static inspection only and will NOT be executed by this workflow."
        )
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            "format": "json",
            "stream": False,
            "think": False,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
                response = client.post(f"{self.base_url}/api/chat", json=request)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
                raise SkillGenerationError("Ollama returned an invalid chat response.")
            content = data["message"].get("content")
            if not isinstance(content, str) or len(content) > (_MAX_TEXT * 2 + 10_000):
                raise SkillGenerationError("Ollama returned missing or oversized content.")
            decoded = json.loads(content)
            if not isinstance(decoded, dict) or set(decoded) != {"source", "tests"}:
                raise SkillGenerationError("Ollama output must contain exactly source and tests.")
            generated = GeneratedSkill(source=decoded["source"], tests=decoded["tests"])
            generated.validate()
            return generated
        except SkillGenerationError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise SkillGenerationError(f"Local Ollama generation failed: {type(exc).__name__}.") from exc


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    message: str
    path: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StaticValidationResult:
    passed: bool
    source_hashes: dict[str, str]
    checked_files: tuple[str, ...]
    findings: tuple[ValidationFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "source_hashes": dict(self.source_hashes),
            "checked_files": list(self.checked_files),
            "findings": [item.to_dict() for item in self.findings],
            "execution_performed": False,
        }


class StaticSkillValidator:
    """Parse source and enforce a restrictive import/API policy without execution."""

    def validate(
        self,
        specification: SkillSpecification,
        files: Mapping[str, str],
    ) -> StaticValidationResult:
        specification.validate()
        if not isinstance(files, Mapping) or not 1 <= len(files) <= _MAX_FILES:
            raise SkillForgeError(f"Candidate must contain between 1 and {_MAX_FILES} files.")
        findings: list[ValidationFinding] = []
        hashes: dict[str, str] = {}
        checked: list[str] = []
        expected_paths = {specification.source_path, specification.test_path}
        if set(files) != expected_paths:
            raise SkillForgeError("Candidate files must be exactly the declared source and test paths.")

        for raw_path, source in files.items():
            path = _safe_relative_path(raw_path)
            if not path.endswith(".py") or not isinstance(source, str) or len(source) > _MAX_TEXT:
                raise SkillForgeError(f"Candidate file is invalid or too large: {path}")
            hashes[path] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            checked.append(path)
            try:
                tree = ast.parse(source, filename=path)
                compile(source, path, "exec")
            except (SyntaxError, ValueError, TypeError) as exc:
                findings.append(ValidationFinding(
                    "syntax-error", f"Python syntax validation failed: {type(exc).__name__}.",
                    path, getattr(exc, "lineno", None),
                ))
                continue
            self._inspect_tree(
                tree, path, findings,
                is_test=(path == specification.test_path),
                candidate_name=specification.name,
                entry_function=specification.entry_point.partition(":")[2],
            )
            self._check_module_scope(tree, path, findings, is_test=(path == specification.test_path))

        source_tree = None
        try:
            source_tree = ast.parse(files[specification.source_path], filename=specification.source_path)
        except (SyntaxError, ValueError, TypeError):
            pass
        if source_tree is not None:
            entry_function = specification.entry_point.partition(":")[2]
            functions = [
                node for node in source_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == entry_function
            ]
            if len(functions) != 1 or isinstance(functions[0], ast.AsyncFunctionDef):
                findings.append(ValidationFinding(
                    "entry-point", "The declared synchronous entry-point function was not found.",
                    specification.source_path,
                ))
            else:
                if not self._signature_matches(functions[0], specification):
                    findings.append(ValidationFinding(
                        "tool-signature",
                        "Entry-point parameters must match the closed JSON Schema, including required and optional fields.",
                        specification.source_path, functions[0].lineno,
                    ))
                decorators = [
                    item for item in functions[0].decorator_list
                    if self._decorator_name(item) == "tool"
                ]
                if len(decorators) != 1 or not isinstance(decorators[0], ast.Call):
                    findings.append(ValidationFinding(
                        "tool-registration", "The entry point must use exactly one @tool(...) decorator.",
                        specification.source_path, functions[0].lineno,
                    ))
                else:
                    decorator = decorators[0]
                    keywords = {
                        item.arg: item.value for item in decorator.keywords
                        if item.arg is not None
                    }
                    required_keywords = {"name", "description", "params_schema"}
                    allowed_keywords = required_keywords | {"mutating"}
                    literal_values = all(
                        item.arg is not None and self._literal_expression(item.value)
                        for item in decorator.keywords
                    )
                    try:
                        declared_name = ast.literal_eval(keywords.get("name"))
                        declared_description = ast.literal_eval(keywords.get("description"))
                        declared_schema = ast.literal_eval(keywords.get("params_schema"))
                        declared_mutating = ast.literal_eval(keywords.get("mutating", ast.Constant(False)))
                    except (ValueError, TypeError):
                        declared_name, declared_description, declared_schema, declared_mutating = None, None, None, None
                    if (
                        decorator.args
                        or not literal_values
                        or not required_keywords.issubset(keywords)
                        or not set(keywords).issubset(allowed_keywords)
                        or declared_name != specification.name
                        or declared_description != specification.description
                        or declared_schema != specification.input_schema
                    ):
                        findings.append(ValidationFinding(
                            "tool-schema", "The @tool decorator must use literal name, description and schema values that exactly match the specification.",
                            specification.source_path, functions[0].lineno,
                        ))
                    if declared_mutating is not False:
                        findings.append(ValidationFinding(
                            "mutating-tool", "Generated Phase 6 tools must be registered as non-mutating.",
                            specification.source_path, functions[0].lineno,
                        ))
            if not any(
                isinstance(node, ast.ImportFrom)
                and node.module == "backtalk.registry"
                and any(alias.name == "tool" for alias in node.names)
                for node in source_tree.body
            ):
                findings.append(ValidationFinding(
                    "registry-import", "Source must import tool from backtalk.registry.",
                    specification.source_path,
                ))

        if specification.required_permissions:
            findings.append(ValidationFinding(
                "permission-denied",
                "No generated-tool permissions are granted in Phase 6; requested permissions remain denied.",
                "skill_specification",
            ))
        if specification.filesystem_scope:
            findings.append(ValidationFinding(
                "filesystem-denied",
                "Generated tools receive no direct filesystem scope in Phase 6.",
                "skill_specification",
            ))
        if specification.network_access_required:
            findings.append(ValidationFinding(
                "network-denied",
                "Network access is disabled for generated tools.",
                "skill_specification",
            ))
        if specification.dependencies:
            findings.append(ValidationFinding(
                "dependencies-denied",
                "Third-party dependencies cannot be installed by Skill Forge.",
                "skill_specification",
            ))

        return StaticValidationResult(
            passed=not findings,
            source_hashes=hashes,
            checked_files=tuple(sorted(checked)),
            findings=tuple(findings),
        )

    @staticmethod
    def _signature_matches(function: ast.FunctionDef, specification: SkillSpecification) -> bool:
        arguments = function.args
        if arguments.posonlyargs or arguments.vararg is not None or arguments.kwarg is not None:
            return False
        names = [item.arg for item in list(arguments.args) + list(arguments.kwonlyargs)]
        properties = specification.input_schema.get("properties", {})
        if (
            not isinstance(properties, dict)
            or specification.input_schema.get("additionalProperties") is not False
            or any(
                not isinstance(name, str)
                or not _PYTHON_NAME.fullmatch(name)
                or name.startswith("__")
                for name in properties
            )
            or set(names) != set(properties)
            or len(names) != len(set(names))
        ):
            return False
        required = set(specification.input_schema.get("required", []))
        positional_defaults = dict(
            zip(
                [item.arg for item in arguments.args[-len(arguments.defaults):]]
                if arguments.defaults else [],
                arguments.defaults,
            )
        )
        keyword_defaults = {
            item.arg: default
            for item, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
            if default is not None
        }
        defaults = {**positional_defaults, **keyword_defaults}
        return all(
            (name in required and name not in defaults)
            or (name not in required and name in defaults)
            for name in names
        )

    @staticmethod
    def _decorator_name(node: ast.expr) -> str | None:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    @staticmethod
    def _safe_annotation(node: ast.expr) -> bool:
        allowed = (
            ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List,
            ast.Constant, ast.BinOp, ast.BitOr, ast.Load, ast.Slice,
        )
        return all(
            isinstance(item, allowed)
            and not (isinstance(item, ast.Attribute) and item.attr.startswith("__"))
            for item in ast.walk(node)
        )

    @staticmethod
    def _literal_expression(node: ast.expr | None) -> bool:
        if node is None:
            return True
        try:
            ast.literal_eval(node)
            return True
        except (ValueError, TypeError):
            return False

    def _inspect_tree(
        self,
        tree: ast.AST,
        path: str,
        findings: list[ValidationFinding],
        *,
        is_test: bool,
        candidate_name: str,
        entry_function: str,
    ) -> None:
        target_module = f"backtalk.features.{candidate_name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    allowed = root in _ALLOWED_IMPORTS or (is_test and root == "unittest")
                    if is_test and alias.name == target_module:
                        allowed = True
                    if not allowed:
                        findings.append(ValidationFinding(
                            "import-denied", f"Import of {alias.name!r} is not permitted.", path, node.lineno
                        ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".", 1)[0]
                allowed = root in _ALLOWED_IMPORTS or (is_test and root == "unittest")
                if module == "backtalk.registry":
                    allowed = not is_test and all(alias.name == "tool" for alias in node.names)
                elif module == "__future__":
                    allowed = all(alias.name == "annotations" for alias in node.names)
                elif is_test and module == target_module:
                    allowed = True
                if node.level or not allowed or any(alias.name == "*" for alias in node.names):
                    findings.append(ValidationFinding(
                        "import-denied", f"Import from {module!r} is not permitted.", path, node.lineno
                    ))
            elif isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                if name in _BLOCKED_CALLS:
                    findings.append(ValidationFinding(
                        "dangerous-call", f"Call to {name!r} is not permitted.", path, node.lineno
                    ))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    allowed_tool_decorator = (
                        not is_test
                        and node in getattr(tree, "body", [])
                        and path.endswith(f"/{candidate_name}.py")
                        and node.name == entry_function
                        and self._decorator_name(decorator) == "tool"
                    )
                    if not allowed_tool_decorator:
                        findings.append(ValidationFinding(
                            "decorator-denied", "Only the entry point may use Lumen's @tool decorator.",
                            path, getattr(decorator, "lineno", node.lineno),
                        ))
                defaults = list(node.args.defaults) + [
                    item for item in node.args.kw_defaults if item is not None
                ]
                if any(not self._literal_expression(item) for item in defaults):
                    findings.append(ValidationFinding(
                        "function-default", "Function defaults must be literal values.",
                        path, node.lineno,
                    ))
                annotations = [
                    item.annotation for item in (
                        list(node.args.posonlyargs)
                        + list(node.args.args)
                        + list(node.args.kwonlyargs)
                    ) if item.annotation is not None
                ]
                if node.args.vararg and node.args.vararg.annotation:
                    annotations.append(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation:
                    annotations.append(node.args.kwarg.annotation)
                if node.returns is not None:
                    annotations.append(node.returns)
                if any(not self._safe_annotation(item) for item in annotations):
                    findings.append(ValidationFinding(
                        "unsafe-annotation", "Annotations must use names, attributes, literals and type subscripts only.",
                        path, node.lineno,
                    ))
            elif isinstance(node, ast.ClassDef):
                allowed_base = (
                    len(node.bases) == 1
                    and isinstance(node.bases[0], ast.Attribute)
                    and isinstance(node.bases[0].value, ast.Name)
                    and node.bases[0].value.id == "unittest"
                    and node.bases[0].attr == "TestCase"
                )
                if not is_test or not allowed_base or node.decorator_list or node.keywords:
                    findings.append(ValidationFinding(
                        "class-definition",
                        "Only undecorated unittest.TestCase classes are permitted in test modules.",
                        path, node.lineno,
                    ))
            elif isinstance(node, ast.Name) and node.id.startswith("__"):
                findings.append(ValidationFinding(
                    "dunder-name", "Dunder name access is not permitted.", path, node.lineno
                ))
            elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                findings.append(ValidationFinding(
                    "dunder-access", "Dunder attribute access is not permitted.", path, node.lineno
                ))
            elif isinstance(node, ast.ClassDef):
                allowed_base = (
                    len(node.bases) == 1
                    and isinstance(node.bases[0], ast.Attribute)
                    and isinstance(node.bases[0].value, ast.Name)
                    and node.bases[0].value.id == "unittest"
                    and node.bases[0].attr == "TestCase"
                )
                if not is_test or not allowed_base or node.decorator_list or node.keywords:
                    findings.append(ValidationFinding(
                        "class-definition",
                        "Only undecorated unittest.TestCase classes are permitted in test modules.",
                        path, node.lineno,
                    ))

    def _check_module_scope(
        self,
        tree: ast.Module,
        path: str,
        findings: list[ValidationFinding],
        *,
        is_test: bool,
    ) -> None:
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is not None and not self._literal_expression(value):
                    findings.append(ValidationFinding(
                        "module-side-effect",
                        "Module-level assignments must contain literal values only.",
                        path, node.lineno,
                    ))
                if isinstance(node, ast.AnnAssign) and not self._safe_annotation(node.annotation):
                    findings.append(ValidationFinding(
                        "unsafe-annotation", "Module-level annotations must be side-effect-free.",
                        path, node.lineno,
                    ))
                continue
            findings.append(ValidationFinding(
                "module-side-effect",
                "Executable module-level statements are not permitted.",
                path, getattr(node, "lineno", None),
            ))
        # Class bodies execute during import. Candidate modules may not define
        # classes; test modules may define only restricted unittest.TestCase classes.
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            allowed_base = (
                len(node.bases) == 1
                and isinstance(node.bases[0], ast.Attribute)
                and isinstance(node.bases[0].value, ast.Name)
                and node.bases[0].value.id == "unittest"
                and node.bases[0].attr == "TestCase"
            )
            if (
                not is_test or not allowed_base or node.decorator_list or node.keywords
            ):
                findings.append(ValidationFinding(
                    "class-definition", "Only undecorated unittest.TestCase classes are permitted in test modules.",
                    path, node.lineno,
                ))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                    continue
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    if not self._literal_expression(child.value):
                        findings.append(ValidationFinding(
                            "class-side-effect",
                            "Class attributes must be literal values.",
                            path, child.lineno,
                        ))
                    if isinstance(child, ast.AnnAssign) and not self._safe_annotation(child.annotation):
                        findings.append(ValidationFinding(
                            "unsafe-annotation", "Class annotations must be side-effect-free.",
                            path, child.lineno,
                        ))
                    continue
                findings.append(ValidationFinding(
                    "class-side-effect",
                    "Class bodies may contain only methods and literals.",
                    path, getattr(child, "lineno", None),
                ))


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    candidate_id: str
    branch: str
    path: str
    base_commit: str


class GitWorktreeManager:
    """Create and manage candidate worktrees without destructive Git cleanup."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        worktree_root: str | Path | None = None,
        timeout_seconds: int = _MAX_GIT_TIMEOUT,
    ) -> None:
        self.repository_root = Path(repository_root).expanduser().resolve(strict=True)
        if not self.repository_root.is_dir():
            raise SkillForgeError("Repository root must be an existing directory.")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 60:
            raise SkillForgeError("Git timeout must be between 1 and 60 seconds.")
        self.timeout_seconds = timeout_seconds
        self.worktree_root = Path(worktree_root).expanduser().absolute() if worktree_root else (
            self.repository_root.parent / f".{self.repository_root.name}-skill-worktrees"
        )
        self._reject_symlink_components(self.worktree_root, stop=self.repository_root.parent)
        try:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillForgeError(f"Cannot create candidate worktree directory: {exc}") from exc
        resolved_worktrees = self.worktree_root.resolve()
        if resolved_worktrees == self.repository_root or self._is_relative_to(resolved_worktrees, self.repository_root):
            raise SkillForgeError("Candidate worktrees must be stored outside the main repository.")
        top = self._git(["rev-parse", "--show-toplevel"], cwd=self.repository_root)
        if Path(top).resolve() != self.repository_root:
            raise SkillForgeError("repository_root must be the Git worktree root.")

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _reject_symlink_components(path: Path, *, stop: Path) -> None:
        absolute = Path(os.path.abspath(path))
        boundary = Path(os.path.abspath(stop))
        current = absolute
        while current != current.parent:
            if current.is_symlink():
                raise SkillForgeError(f"Symlink path components are not allowed: {current}")
            if current == boundary:
                return
            current = current.parent
        if GitWorktreeManager._is_relative_to(absolute, boundary) is False and absolute != boundary:
            raise SkillForgeError("Worktree path must remain under the designated parent directory.")

    def _git(self, arguments: list[str], *, cwd: Path) -> str:
        try:
            process = subprocess.run(
                ["git", *arguments],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SkillForgeError(f"Git operation failed: {type(exc).__name__}.") from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "Git returned an error")[:_MAX_GIT_OUTPUT]
            raise SkillForgeError(f"Git operation failed: {detail.strip()}")
        return process.stdout.strip()[:_MAX_GIT_OUTPUT]

    def resolve_head(self) -> str:
        commit = self._git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=self.repository_root)
        if not _COMMIT.fullmatch(commit):
            raise SkillForgeError("Git returned an invalid HEAD commit.")
        return commit.lower()

    def create(self, candidate_id: str, *, base_commit: str | None = None) -> WorktreeInfo:
        if not isinstance(candidate_id, str) or not _SKILL_ID.fullmatch(candidate_id):
            raise SkillForgeError("Invalid candidate ID for worktree creation.")
        status = self._git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=self.repository_root
        )
        if status:
            raise SkillForgeError("Main checkout is dirty; refusing to create a candidate worktree.")
        commit_input = base_commit or self.resolve_head()
        if not isinstance(commit_input, str) or not _COMMIT.fullmatch(commit_input):
            raise SkillForgeError("base_commit must be a hexadecimal Git commit ID.")
        base = self._git(
            ["rev-parse", "--verify", f"{commit_input}^{{commit}}"], cwd=self.repository_root
        ).lower()
        if not _COMMIT.fullmatch(base):
            raise SkillForgeError("Git did not resolve base_commit to a commit.")
        branch = f"skillforge/{candidate_id}"
        self._git(["check-ref-format", "--branch", branch], cwd=self.repository_root)
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(self.repository_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=self.timeout_seconds,
            check=False,
            shell=False,
        )
        if exists.returncode == 0:
            raise SkillForgeError("Candidate branch already exists; refusing to overwrite it.")
        if exists.returncode != 1:
            raise SkillForgeError("Could not determine whether the candidate branch already exists.")
        target = self.worktree_root / candidate_id
        if target.exists() or target.is_symlink():
            raise SkillForgeError("Candidate worktree path already exists.")
        try:
            self._git(
                ["worktree", "add", "-b", branch, str(target), base],
                cwd=self.repository_root,
            )
        except SkillForgeError:
            # Never recursively delete a partially-created worktree. Surface
            # its path so an operator can inspect it and recover safely.
            raise
        resolved = target.resolve(strict=True)
        if not self._is_relative_to(resolved, self.worktree_root.resolve()):
            raise SkillForgeError("Git created a worktree outside the designated directory.")
        return WorktreeInfo(candidate_id, branch, str(resolved), base)

    def _validated_worktree_root(self, worktree: WorktreeInfo) -> Path:
        if (
            not isinstance(worktree, WorktreeInfo)
            or not isinstance(worktree.candidate_id, str)
            or not _SKILL_ID.fullmatch(worktree.candidate_id)
            or worktree.branch != f"skillforge/{worktree.candidate_id}"
        ):
            raise SkillForgeError("Candidate worktree identity is invalid.")
        self._reject_symlink_components(
            self.worktree_root, stop=self.repository_root.parent
        )
        try:
            worktree_root = self.worktree_root.resolve(strict=True)
            candidate_path = worktree_root / worktree.candidate_id
            if candidate_path.is_symlink():
                raise SkillForgeError("Candidate worktree root cannot be a symlink.")
            expected = candidate_path.resolve(strict=True)
            root = Path(worktree.path).resolve(strict=True)
        except OSError as exc:
            raise SkillForgeError("Candidate worktree is missing or inaccessible.") from exc
        if root != expected or not self._is_relative_to(root, worktree_root):
            raise SkillForgeError("Candidate worktree path does not match its candidate ID.")
        top = self._git(["rev-parse", "--show-toplevel"], cwd=root)
        if Path(top).resolve() != root:
            raise SkillForgeError("Candidate path is not the root of its Git worktree.")
        branch = self._git(["branch", "--show-current"], cwd=root)
        if branch != worktree.branch:
            raise SkillForgeError("Candidate Git worktree is no longer on its assigned branch.")
        repository_common = Path(
            self._git(["rev-parse", "--git-common-dir"], cwd=self.repository_root)
        )
        candidate_common = Path(
            self._git(["rev-parse", "--git-common-dir"], cwd=root)
        )
        if not repository_common.is_absolute():
            repository_common = self.repository_root / repository_common
        if not candidate_common.is_absolute():
            candidate_common = root / candidate_common
        if repository_common.resolve() != candidate_common.resolve():
            raise SkillForgeError("Candidate worktree does not belong to the main repository.")
        return root

    def _worktree_file(self, worktree: WorktreeInfo, relative_path: str) -> Path:
        root = self._validated_worktree_root(worktree)
        safe = _safe_relative_path(relative_path)
        target = root.joinpath(*PurePosixPath(safe).parts)
        current = root
        for part in PurePosixPath(safe).parts:
            current = current / part
            if current.is_symlink():
                raise SkillForgeError("Candidate paths cannot contain symlinks.")
        resolved_parent = target.parent.resolve()
        if not self._is_relative_to(resolved_parent, root):
            raise SkillForgeError("Candidate path escaped its worktree.")
        return target

    def write_file(self, worktree: WorktreeInfo, relative_path: str, content: str) -> None:
        if not isinstance(content, str) or len(content) > _MAX_TEXT:
            raise SkillForgeError("Candidate file content is invalid or too large.")
        target = self._worktree_file(worktree, relative_path)
        if target.exists():
            raise SkillForgeError("Candidate file already exists; refusing to overwrite it.")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after directory creation to reduce path-substitution risk.
            target = self._worktree_file(worktree, relative_path)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise SkillForgeError(f"Could not write candidate file: {type(exc).__name__}.") from exc

    def read_file(self, worktree: WorktreeInfo, relative_path: str) -> str:
        target = self._worktree_file(worktree, relative_path)
        if not target.is_file() or target.is_symlink():
            raise SkillForgeError("Candidate review file is missing or unsafe.")
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillForgeError("Could not read candidate review file.") from exc
        if len(content) > _MAX_TEXT:
            raise SkillForgeError("Candidate review file exceeds the size limit.")
        return content

    def abandon(self, worktree: WorktreeInfo) -> None:
        root = self._validated_worktree_root(worktree)
        status = self._git(
            ["status", "--porcelain", "--untracked-files=all", "--ignored"], cwd=root
        )
        if status:
            raise SkillForgeError(
                "Candidate worktree contains changes or untracked files; inspect it before removal."
            )
        self._git(["worktree", "remove", str(root)], cwd=self.repository_root)
        # The candidate branch is deliberately retained for audit/recovery.


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Evidence returned only by a real restricted execution backend."""

    receipt_id: str
    candidate_id: str
    source_sha256: str
    status: str
    passed: bool
    security_boundary: bool
    checks: tuple[str, ...]
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["checks"] = list(self.checks)
        return result


class CandidateExecutionBackend(Protocol):
    available: bool

    def run_candidate(
        self, worktree_path: str, *, candidate_id: str, source_sha256: str
    ) -> ExecutionReceipt:
        """Run candidate checks in a restricted OS security boundary."""


class UnavailableExecutionBackend:
    """Fail-closed placeholder; performs no process creation or candidate execution."""

    available = False

    def run_candidate(
        self, worktree_path: str, *, candidate_id: str, source_sha256: str
    ) -> ExecutionReceipt:
        raise SkillForgeError("No restricted candidate-execution backend is configured.")


@dataclass(frozen=True, slots=True)
class HumanReviewRecord:
    review_id: str
    candidate_id: str
    source_sha256: str
    decision: str
    reason: str
    reviewed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InstallationApproval:
    approval_id: str
    candidate_id: str
    source_sha256: str
    review_id: str
    execution_receipt_id: str
    approved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkillReviewPacket:
    candidate_id: str
    name: str
    description: str
    source_sha256: str
    source_files: dict[str, str]
    diff: str
    requested_permissions: tuple[str, ...]
    filesystem_scope: tuple[str, ...]
    network_access_required: bool
    dependencies: tuple[str, ...]
    static_validation: dict[str, Any]
    runtime_status: str
    review_status: str
    installation_status: str
    activation_status: str


class SkillForge:
    """Prepare reviewable candidates; never install, load, or activate them."""

    def __init__(
        self,
        repository_root: str | Path,
        archive: CandidateArchive,
        *,
        generator: SkillGenerator | None = None,
        execution_backend: CandidateExecutionBackend | None = None,
        worktree_root: str | Path | None = None,
    ) -> None:
        self.worktrees = GitWorktreeManager(repository_root, worktree_root=worktree_root)
        if not isinstance(archive, CandidateArchive):
            raise SkillForgeError("SkillForge requires the existing CandidateArchive.")
        self.archive = archive
        self.generator = generator or OllamaSkillGenerator()
        self.execution_backend = execution_backend or UnavailableExecutionBackend()
        self.validator = StaticSkillValidator()
        self.review_root = archive.root / "skill_forge"
        try:
            self.review_root.mkdir(parents=True, exist_ok=True)
            if self.review_root.is_symlink() or self.review_root.resolve().parent != archive.root:
                raise SkillForgeError("Skill Forge review ledger must be a regular archive subdirectory.")
        except OSError as exc:
            raise SkillForgeError(f"Could not initialise Skill Forge review ledger: {exc}") from exc

    def _ledger_folder(self, name: str) -> Path:
        if name not in {"reviews", "install_approvals", "execution_receipts"}:
            raise SkillForgeError("Unknown Skill Forge ledger.")
        folder = self.review_root / name
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or folder.resolve().parent != self.review_root.resolve():
            raise SkillForgeError("Skill Forge ledger path changed or is unsafe.")
        return folder

    def _append_ledger(self, name: str, payload: dict[str, Any], record_id: str) -> None:
        if not isinstance(record_id, str) or not _SKILL_ID.fullmatch(record_id):
            raise SkillForgeError("Skill Forge ledger record ID must be a 32-character hexadecimal ID.")
        if not isinstance(payload, dict):
            raise SkillForgeError("Skill Forge ledger records must be JSON objects.")
        folder = self._ledger_folder(name)
        target = folder / f"{record_id}.json"
        temporary = folder / f".{record_id}.{uuid.uuid4().hex}.tmp"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if len(encoded.encode("utf-8")) > _MAX_LEDGER_BYTES:
            raise SkillForgeError("Skill Forge ledger record exceeds the size limit.")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise SkillForgeError("Skill Forge ledger records are append-only.")
            os.rename(temporary, target)
        except (OSError, ValueError, TypeError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, SkillForgeError):
                raise
            raise SkillForgeError("Could not append Skill Forge audit record.") from exc

    def _ledger_records(self, name: str) -> list[dict[str, Any]]:
        folder = self._ledger_folder(name)
        id_field = {
            "reviews": "review_id",
            "install_approvals": "approval_id",
            "execution_receipts": "receipt_id",
        }[name]
        records: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json")):
            if path.is_symlink() or path.stat().st_size > _MAX_LEDGER_BYTES:
                raise SkillForgeError("Unsafe or oversized Skill Forge ledger record.")
            try:
                if not _SKILL_ID.fullmatch(path.stem):
                    raise ValueError("record filename is not a valid ID")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get(id_field) != path.stem
                ):
                    raise ValueError("record ID does not match its immutable filename")
                records.append(payload)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise SkillForgeError("Could not read Skill Forge audit record.") from exc
        return records

    def prepare_candidate(
        self,
        specification: SkillSpecification,
        *,
        base_commit: str | None = None,
    ) -> CandidateRecord:
        """Generate into a fresh worktree, statically inspect, and archive metadata."""
        specification.validate()
        if specification.parent_candidate_id is not None:
            parent = self.archive.get_candidate(specification.parent_candidate_id)
            decision = self.archive.latest_decision(parent.candidate_id)
            evaluation = self.archive.latest_evaluation(parent.candidate_id)
            if (
                decision is None
                or decision.decision not in {"retain", "promote"}
                or evaluation is None
                or evaluation.passed is not True
            ):
                raise SkillForgeError(
                    "A parent must have a passing evaluation and a retain/promote archive decision."
                )
        generated = self.generator.generate(specification)
        if not isinstance(generated, GeneratedSkill):
            raise SkillGenerationError("Generator must return GeneratedSkill.")
        generated.validate()
        files = {
            specification.source_path: generated.source,
            specification.test_path: generated.tests,
        }
        validation = self.validator.validate(specification, files)
        base = base_commit or self.worktrees.resolve_head()
        worktree = self.worktrees.create(specification.candidate_id, base_commit=base)
        try:
            for path, content in files.items():
                self.worktrees.write_file(worktree, path, content)
            manifest = {
                "schema_version": 1,
                "candidate_id": specification.candidate_id,
                "created_at": _now(),
                "generator": getattr(self.generator, "name", type(self.generator).__name__),
                "generator_model": getattr(self.generator, "model", None),
                "base_commit": worktree.base_commit,
                "branch": worktree.branch,
                "worktree_ref": specification.candidate_id,
                "entry_point": specification.entry_point,
                "specification": specification.to_dict(),
                "source_hashes": validation.source_hashes,
                "static_validation": validation.to_dict(),
                "runtime_status": "awaiting_isolated_execution",
                "human_review_status": "pending_review",
                "installation_status": "not_approved",
                "activation_status": "not_installed",
            }
            manifest_path = "backtalk/SKILL_FORGE_MANIFEST.json"
            self.worktrees.write_file(
                worktree, manifest_path,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            changed_files = tuple(sorted((*files.keys(), manifest_path)))
            metadata = {
                "skill_forge": {
                    "schema_version": 1,
                    "base_commit": worktree.base_commit,
                    "worktree_ref": specification.candidate_id,
                    "generator": manifest["generator"],
                    "generator_model": manifest["generator_model"],
                    "specification": specification.to_dict(),
                    "manifest_path": manifest_path,
                    "source_hashes": validation.source_hashes,
                    "static_validation": validation.to_dict(),
                    "runtime_status": "awaiting_isolated_execution",
                    "human_review_status": "pending_review",
                    "installation_status": "not_approved",
                    "activation_status": "not_installed",
                }
            }
            return self.archive.add_candidate(
                worktree.path,
                source_branch=worktree.branch,
                improvement_summary=specification.description,
                suite_version=_SUITE_VERSION,
                parent_candidate_id=specification.parent_candidate_id,
                source_commit=None,
                changed_files=changed_files,
                metadata=metadata,
                candidate_id=specification.candidate_id,
            )
        except Exception:
            # Generated, untracked files are deliberately not recursively
            # removed. Keep the branch/worktree for explicit inspection.
            raise

    def review_candidate(self, candidate_id: str) -> SkillReviewPacket:
        candidate = self.archive.get_candidate(candidate_id)
        details = candidate.metadata.get("skill_forge")
        if not isinstance(details, dict):
            raise SkillForgeError("Candidate does not contain Skill Forge metadata.")
        worktree = WorktreeInfo(
            candidate_id=candidate.candidate_id,
            branch=candidate.source_branch,
            path=str(self.worktrees.worktree_root / candidate.candidate_id),
            base_commit=str(details.get("base_commit") or ""),
        )
        try:
            validated_root = self.worktrees._validated_worktree_root(worktree)
            current_digest = project_tree_digest(validated_root)
        except Exception as exc:
            raise SkillForgeError("Candidate worktree is missing or cannot be inspected.") from exc
        if current_digest != candidate.source_sha256:
            raise SkillForgeError("Candidate source changed after archiving; review evidence is stale.")
        spec_data = details.get("specification")
        if not isinstance(spec_data, dict) or details.get("worktree_ref") != candidate.candidate_id:
            raise SkillForgeError("Candidate specification or worktree reference is missing.")
        try:
            specification = SkillSpecification(**spec_data)
            specification.validate()
        except (TypeError, ValueError) as exc:
            raise SkillForgeError("Candidate specification metadata is invalid.") from exc
        if specification.candidate_id != candidate.candidate_id:
            raise SkillForgeError("Candidate specification ID does not match the archive record.")
        source_path = specification.source_path
        test_path = specification.test_path
        manifest_path = _safe_relative_path(details.get("manifest_path") or "")
        files = {
            source_path: self.worktrees.read_file(worktree, source_path),
            test_path: self.worktrees.read_file(worktree, test_path),
            manifest_path: self.worktrees.read_file(worktree, manifest_path),
        }
        # Candidate files are created exclusively and never overwrite base files,
        # so their review diff consists only of additions from /dev/null.
        diff_parts: list[str] = []
        for relative_path, content in sorted(files.items()):
            diff_parts.extend(difflib.unified_diff(
                (),
                content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{relative_path}",
                lineterm="\n",
            ))
        review_diff = "".join(diff_parts)
        latest_review = self._latest_review(candidate_id)
        latest_execution = self._latest_execution(candidate_id)
        review_status = latest_review.decision if latest_review else "pending_review"
        runtime_status = (
            str(latest_execution.get("status"))
            if latest_execution is not None
            else str(details.get("runtime_status") or "unknown")
        )
        installation_status = (
            "approved_for_installation"
            if self._has_installation_approval(candidate_id, candidate.source_sha256)
            else "not_approved"
        )
        return SkillReviewPacket(
            candidate_id=candidate.candidate_id,
            name=specification.name,
            description=specification.description,
            source_sha256=candidate.source_sha256,
            source_files=files,
            diff=review_diff,
            requested_permissions=tuple(specification.required_permissions),
            filesystem_scope=tuple(specification.filesystem_scope),
            network_access_required=specification.network_access_required,
            dependencies=tuple(specification.dependencies),
            static_validation=dict(details.get("static_validation") or {}),
            runtime_status=runtime_status,
            review_status=review_status,
            installation_status=installation_status,
            activation_status="not_installed",
        )

    def _latest_review(self, candidate_id: str) -> HumanReviewRecord | None:
        records = [
            HumanReviewRecord(**item)
            for item in self._ledger_records("reviews")
            if item.get("candidate_id") == candidate_id
        ]
        return max(records, key=lambda item: item.reviewed_at) if records else None

    def _latest_execution(self, candidate_id: str) -> dict[str, Any] | None:
        records = [
            item for item in self._ledger_records("execution_receipts")
            if item.get("candidate_id") == candidate_id
        ]
        return max(records, key=lambda item: str(item.get("completed_at") or "")) if records else None

    def _has_installation_approval(self, candidate_id: str, source_sha256: str) -> bool:
        return any(
            item.get("candidate_id") == candidate_id
            and item.get("source_sha256") == source_sha256
            for item in self._ledger_records("install_approvals")
        )

    def record_human_review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: str,
    ) -> HumanReviewRecord:
        """Append an explicit human code-review decision; this never installs code."""
        packet = self.review_candidate(candidate_id)
        if not isinstance(decision, str) or decision not in {"approved", "rejected"}:
            raise SkillForgeError("Human review decision must be approved or rejected.")
        reason_text = _bounded_text(reason, "review reason")
        if decision == "approved" and packet.static_validation.get("passed") is not True:
            raise SkillForgeError("A candidate with failed static validation cannot be approved.")
        record = HumanReviewRecord(
            review_id=uuid.uuid4().hex,
            candidate_id=candidate_id,
            source_sha256=packet.source_sha256,
            decision=decision,
            reason=reason_text,
            reviewed_at=_now(),
        )
        self._append_ledger("reviews", record.to_dict(), record.review_id)
        return record

    def evaluate_candidate(self, candidate_id: str) -> ExecutionReceipt:
        """Execute only after digest-bound human review and through a restricted backend."""
        packet = self.review_candidate(candidate_id)
        review = self._latest_review(candidate_id)
        if (
            review is None
            or review.decision != "approved"
            or review.source_sha256 != packet.source_sha256
        ):
            raise SkillForgeError("A current human code-review approval is required before candidate execution.")
        if not getattr(self.execution_backend, "available", False):
            return ExecutionReceipt(
                receipt_id="",
                candidate_id=candidate_id,
                source_sha256=packet.source_sha256,
                status="awaiting_isolated_execution",
                passed=False,
                security_boundary=False,
                checks=(),
                completed_at=_now(),
            )
        candidate = self.archive.get_candidate(candidate_id)
        details = candidate.metadata["skill_forge"]
        receipt = self.execution_backend.run_candidate(
            str(self.worktrees.worktree_root / candidate.candidate_id),
            candidate_id=candidate_id,
            source_sha256=candidate.source_sha256,
        )
        if (
            not isinstance(receipt, ExecutionReceipt)
            or not isinstance(receipt.receipt_id, str)
            or not _SKILL_ID.fullmatch(receipt.receipt_id)
            or receipt.candidate_id != candidate_id
            or receipt.source_sha256 != candidate.source_sha256
            or receipt.security_boundary is not True
            or not isinstance(receipt.passed, bool)
            or not isinstance(receipt.status, str)
            or receipt.status not in {"passed", "failed", "timed_out", "error"}
            or (receipt.passed is True) != (receipt.status == "passed")
            or not isinstance(receipt.checks, (tuple, list))
            or any(not isinstance(item, str) for item in receipt.checks)
        ):
            raise SkillForgeError("Restricted backend returned an invalid candidate receipt.")
        if receipt.passed and not {"compileall", "unittest"}.issubset(set(receipt.checks)):
            raise SkillForgeError("A passing runtime receipt must include compileall and unittest checks.")
        self._append_ledger("execution_receipts", receipt.to_dict(), receipt.receipt_id)
        return receipt

    def approve_installation(
        self,
        candidate_id: str,
        *,
        source_sha256: str,
        runtime_receipt: ExecutionReceipt,
        human_confirmed: bool,
    ) -> InstallationApproval:
        """Record approval for a later installer; this method does not install or activate."""
        packet = self.review_candidate(candidate_id)
        review = self._latest_review(candidate_id)
        if human_confirmed is not True:
            raise SkillForgeError("Installation approval requires an explicit human confirmation.")
        if review is None or review.decision != "approved":
            raise SkillForgeError("A current human code-review approval is required.")
        if source_sha256 != packet.source_sha256 or review.source_sha256 != packet.source_sha256:
            raise SkillForgeError("Source changed after review; approval is invalid.")
        if (
            not isinstance(runtime_receipt, ExecutionReceipt)
            or runtime_receipt.candidate_id != candidate_id
            or runtime_receipt.source_sha256 != packet.source_sha256
            or runtime_receipt.status != "passed"
            or runtime_receipt.passed is not True
            or runtime_receipt.security_boundary is not True
            or not isinstance(runtime_receipt.receipt_id, str)
            or not _SKILL_ID.fullmatch(runtime_receipt.receipt_id)
            or not {"compileall", "unittest"}.issubset(set(runtime_receipt.checks))
        ):
            raise SkillForgeError("A passing receipt from a restricted execution backend is required.")
        recorded = next(
            (item for item in reversed(self._ledger_records("execution_receipts"))
             if item.get("receipt_id") == runtime_receipt.receipt_id),
            None,
        )
        if recorded != runtime_receipt.to_dict():
            raise SkillForgeError("Runtime receipt was not recorded by this Skill Forge backend.")
        record = InstallationApproval(
            approval_id=uuid.uuid4().hex,
            candidate_id=candidate_id,
            source_sha256=packet.source_sha256,
            review_id=review.review_id,
            execution_receipt_id=runtime_receipt.receipt_id,
            approved_at=_now(),
        )
        self._append_ledger("install_approvals", record.to_dict(), record.approval_id)
        return record

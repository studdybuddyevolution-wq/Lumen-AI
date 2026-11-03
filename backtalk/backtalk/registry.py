"""Validated tool registry and explicit-trust hot reload for Lumen.

Feature modules are trusted local Python extensions, not sandboxed code.
Built-in modules are part of the application. Additional modules load only
when their adjacent .approved file contains the module's current SHA-256.
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import importlib.machinery
import importlib.util
import logging
import re
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, SchemaError, SchemaError as _UnusedSchemaError

LOGGER = logging.getLogger("lumen.registry")
_TOOL_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_ACTIVE_REGISTRY: contextvars.ContextVar["ToolRegistry | None"] = (
    contextvars.ContextVar("lumen_active_tool_registry", default=None)
)
_BUILTIN_FEATURES = {"core_tools.py"}


class ToolRegistrationError(ValueError):
    """Raised when a tool cannot be added to the registry."""


class ToolValidationError(ValueError):
    """Raised when model-supplied arguments do not match the tool schema."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    params_schema: dict[str, Any]
    mutating: bool
    handler: Callable[..., Any]
    source: str

    def to_ollama_declaration(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.params_schema),
            },
        }


def approve_feature_module(path: str | Path) -> str:
    """Write a SHA-256 approval sidecar after a human has reviewed the code.

    The caller must obtain explicit user approval before invoking this helper.
    """
    feature_path = Path(path).resolve()
    if feature_path.suffix != ".py" or not feature_path.is_file():
        raise ValueError("Feature approval requires an existing .py file.")
    digest = hashlib.sha256(feature_path.read_bytes()).hexdigest()
    feature_path.with_suffix(".approved").write_text(digest + "\n", encoding="ascii")
    return digest


class ToolRegistry:
    """Thread-safe registry for built-in and dynamically loaded local tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._source_tools: dict[str, set[str]] = {}
        self._modules: dict[str, types.ModuleType] = {}
        self._feature_dir: Path | None = None
        self._signatures: dict[str, tuple[int, int, str]] = {}
        self._generation = 0
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    @staticmethod
    def _check_schema(schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ToolRegistrationError(
                "params_schema must be a JSON Schema object with type='object'."
            )
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise ToolRegistrationError(f"Invalid JSON Schema: {exc}") from exc

    def register(
        self,
        *,
        name: str,
        description: str,
        params_schema: dict[str, Any],
        mutating: bool,
        handler: Callable[..., Any],
        source: str = "builtin",
    ) -> ToolDefinition:
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ToolRegistrationError("Tool name is invalid or too long.")
        if not isinstance(description, str) or not description.strip():
            raise ToolRegistrationError(f"Tool {name!r} needs a description.")
        if not callable(handler):
            raise ToolRegistrationError(f"Tool {name!r} handler must be callable.")
        if not isinstance(mutating, bool):
            raise ToolRegistrationError(f"Tool {name!r} mutating must be boolean.")
        self._check_schema(params_schema)

        definition = ToolDefinition(
            name=name,
            description=description.strip(),
            params_schema=copy.deepcopy(params_schema),
            mutating=mutating,
            handler=handler,
            source=source,
        )
        with self._lock:
            existing = self._tools.get(name)
            if existing is not None:
                raise ToolRegistrationError(
                    f"Tool {name!r} already belongs to {existing.source!r}."
                )
            self._tools[name] = definition
            self._source_tools.setdefault(source, set()).add(name)
        return definition

    def unregister_source(self, source: str) -> None:
        with self._lock:
            for name in self._source_tools.pop(source, set()):
                current = self._tools.get(name)
                if current is not None and current.source == source:
                    self._tools.pop(name, None)

    def get(self, name: str) -> ToolDefinition | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        with self._lock:
            return sorted(self._tools.values(), key=lambda item: item.name)

    def declarations(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            selected = (
                list(self._tools.values())
                if names is None
                else [self._tools[name] for name in names if name in self._tools]
            )
        return [item.to_ollama_declaration() for item in selected]

    def validate_arguments(self, name: str, arguments: Any) -> dict[str, Any]:
        definition = self.get(name)
        if definition is None:
            raise ToolValidationError(f"Unknown tool: {name!r}.")
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        validator = Draft202012Validator(definition.params_schema)
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.absolute_path) or "$"
            raise ToolValidationError(f"{location}: {first.message}")
        return arguments

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        definition = self.get(name)
        if definition is None:
            raise ToolValidationError(f"Unknown tool: {name!r}.")
        clean_arguments = self.validate_arguments(name, arguments)
        result = definition.handler(**clean_arguments)
        if hasattr(result, "__await__"):
            raise TypeError("Tool handlers must be synchronous functions.")
        return result

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return stat.st_mtime_ns, stat.st_size, digest

    @staticmethod
    def _is_approved(path: Path, digest: str) -> bool:
        if path.name in _BUILTIN_FEATURES:
            return True
        try:
            stored = path.with_suffix(".approved").read_text(
                encoding="ascii"
            ).strip().lower()
        except OSError:
            return False
        return stored == digest

    def _ensure_feature_package(self, feature_dir: Path) -> str:
        package_name = "_lumen_feature_plugins_" + str(
            abs(hash(str(feature_dir.resolve())))
        )
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(feature_dir)]  # type: ignore[attr-defined]
            package.__package__ = package_name
            package.__spec__ = importlib.machinery.ModuleSpec(
                package_name, loader=None, is_package=True
            )
            sys.modules[package_name] = package
        return package_name

    def _load_feature(self, path: Path, package_name: str) -> bool:
        source = str(path.resolve())
        previous = {
            name: definition
            for name, definition in self._tools.items()
            if definition.source == source
        }
        self.unregister_source(source)
        self._generation += 1
        module_name = f"{package_name}.{path.stem}_{self._generation}"
        token = _ACTIVE_REGISTRY.set(self)
        module: types.ModuleType | None = None
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not create import spec for {path.name}")
            module = importlib.util.module_from_spec(spec)
            module.__package__ = package_name
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for old_name, old_module in list(self._modules.items()):
                if Path(str(getattr(old_module, "__file__", ""))).resolve() == path:
                    self._modules.pop(old_name, None)
                    sys.modules.pop(old_name, None)
            self._modules[module_name] = module
            LOGGER.info("Loaded feature module %s", path.name)
            return True
        except Exception:
            if module is not None:
                sys.modules.pop(module_name, None)
            with self._lock:
                for name, definition in previous.items():
                    self._tools[name] = definition
                    self._source_tools.setdefault(source, set()).add(name)
            LOGGER.exception(
                "Feature module %s failed to load; previous tools retained", path.name
            )
            return False
        finally:
            _ACTIVE_REGISTRY.reset(token)

    def load_features(self, feature_dir: str | Path) -> dict[str, str]:
        """Load changed approved modules; preserve working versions on bad edits."""
        folder = Path(feature_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").touch(exist_ok=True)
        package_name = self._ensure_feature_package(folder)
        results: dict[str, str] = {}
        current: dict[str, tuple[int, int, str]] = {}

        for path in sorted(folder.glob("*.py")):
            if path.name == "__init__.py" or not path.stem.isidentifier():
                continue
            try:
                signature = self._signature(path)
            except OSError as exc:
                results[path.name] = f"unavailable: {exc}"
                continue

            old_signature = self._signatures.get(path.name)
            if old_signature == signature:
                current[path.name] = signature
                results[path.name] = "unchanged"
                continue

            if not self._is_approved(path, signature[2]):
                results[path.name] = "skipped: missing or stale approval hash"
                if old_signature is not None:
                    current[path.name] = old_signature
                continue

            if self._load_feature(path, package_name):
                current[path.name] = signature
                results[path.name] = "loaded"
            else:
                if old_signature is not None:
                    current[path.name] = old_signature
                results[path.name] = "error: previous version retained"

        present_names = {path.name for path in folder.glob("*.py")}
        for old_name in set(self._signatures) - present_names:
            old_source = str((folder / old_name).resolve())
            self.unregister_source(old_source)
            for module_name, module in list(self._modules.items()):
                if Path(str(getattr(module, "__file__", ""))).resolve() == Path(old_source):
                    self._modules.pop(module_name, None)
                    sys.modules.pop(module_name, None)
            results[old_name] = "removed"

        self._feature_dir = folder
        self._signatures = current
        return results

    def start_watcher(
        self, feature_dir: str | Path | None = None, *, interval_seconds: float = 1.0
    ) -> None:
        folder = Path(feature_dir).resolve() if feature_dir else self._feature_dir
        if folder is None:
            raise ValueError("Call load_features() before starting the feature watcher.")
        self._feature_dir = folder
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()

        def watch() -> None:
            while not self._watch_stop.wait(max(0.25, interval_seconds)):
                try:
                    self.load_features(folder)
                except Exception:
                    LOGGER.exception("Feature watcher scan failed")

        self._watch_thread = threading.Thread(
            target=watch, name="lumen-feature-watcher", daemon=True
        )
        self._watch_thread.start()

    def stop_watcher(self, timeout: float = 2.0) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._watch_thread = None


DEFAULT_REGISTRY = ToolRegistry()


def tool(
    name: str,
    description: str,
    params_schema: dict[str, Any],
    mutating: bool = False,
    *,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for registering a feature function as a Lumen tool."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        active = registry or _ACTIVE_REGISTRY.get() or DEFAULT_REGISTRY
        module = sys.modules.get(function.__module__)
        module_file = getattr(module, "__file__", None)
        source = (
            str(Path(module_file).resolve()) if module_file else function.__module__
        )
        active.register(
            name=name,
            description=description,
            params_schema=params_schema,
            mutating=mutating,
            handler=function,
            source=source,
        )
        return function
    return decorate

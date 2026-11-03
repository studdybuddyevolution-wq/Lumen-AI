"""Validated tool registration and hot-reload support for Lumen.

Feature modules are trusted local extensions. The registry validates their
metadata and tool arguments; it is not a sandbox for untrusted Python code.
"""
from __future__ import annotations

import contextvars
import copy
import importlib
import importlib.machinery
import logging
import re
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, SchemaError, ValidationError

LOGGER = logging.getLogger("lumen.registry")
_TOOL_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_ACTIVE_REGISTRY: contextvars.ContextVar["ToolRegistry | None"] = (
    contextvars.ContextVar("lumen_active_tool_registry", default=None)
)


class ToolRegistrationError(ValueError):
    """Raised when a tool cannot safely be added to the registry."""


class ToolValidationError(ValueError):
    """Raised when a model-supplied argument object does not match a schema."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    params_schema: dict[str, Any]
    mutating: bool
    handler: Callable[..., Any]
    source: str

    def to_ollama_declaration(self) -> dict[str, Any]:
        """Return the function-tool shape expected by Ollama's /api/chat."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.params_schema),
            },
        }


class ToolRegistry:
    """Thread-safe registry for built-in and dynamically loaded local tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._source_tools: dict[str, set[str]] = {}
        self._modules: dict[str, types.ModuleType] = {}
        self._module_sources: dict[str, str] = {}
        self._feature_dir: Path | None = None
        self._signatures: dict[str, tuple[int, int]] = {}
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    @staticmethod
    def _check_schema(schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            raise ToolRegistrationError("params_schema must be a JSON Schema object.")
        if schema.get("type") != "object":
            raise ToolRegistrationError("Tool params_schema must have type='object'.")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ToolRegistrationError(f"Invalid JSON Schema: {exc.message}") from exc

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
        """Register one tool. Duplicate names fail rather than shadow tools."""
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ToolRegistrationError(
                "Tool names must start with a letter and contain only letters, "
                "numbers, or underscores (maximum 64 characters)."
            )
        if not isinstance(description, str) or not description.strip():
            raise ToolRegistrationError(f"Tool {name!r} needs a non-empty description.")
        if not callable(handler):
            raise ToolRegistrationError(f"Tool {name!r} handler must be callable.")
        if not isinstance(mutating, bool):
            raise ToolRegistrationError(f"Tool {name!r} mutating must be a boolean.")
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
                    f"Tool name {name!r} is already registered by {existing.source!r}."
                )
            self._tools[name] = definition
            self._source_tools.setdefault(source, set()).add(name)
        return definition

    def unregister_source(self, source: str) -> None:
        """Remove all tools contributed by one feature source."""
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
        """Build Ollama declarations, optionally restricted to named tools."""
        with self._lock:
            if names is None:
                selected = list(self._tools.values())
            else:
                selected = [
                    self._tools[name] for name in names if name in self._tools
                ]
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
        """Validate then invoke a registered synchronous tool handler."""
        definition = self.get(name)
        if definition is None:
            raise ToolValidationError(f"Unknown tool: {name!r}.")
        clean_arguments = self.validate_arguments(name, arguments)
        result = definition.handler(**clean_arguments)
        if hasattr(result, "__await__"):
            raise TypeError(
                f"Tool {name!r} returned an awaitable; registry handlers must be synchronous."
            )
        return result

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

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

    def _load_feature(self, path: Path, package_name: str) -> None:
        source = str(path.resolve())
        module_name = f"{package_name}.{path.stem}"
        previous = {
            name: definition
            for name, definition in self._tools.items()
            if definition.source == source
        }
        self.unregister_source(source)
        token = _ACTIVE_REGISTRY.set(self)
        try:
            importlib.invalidate_caches()
            if module_name in self._modules:
                module = importlib.reload(self._modules[module_name])
            else:
                module = importlib.import_module(module_name)
            self._modules[module_name] = module
            self._module_sources[module_name] = source
            LOGGER.info("Loaded feature module %s", path.name)
        except Exception:
            # Preserve the last known-good feature version if reload fails.
            with self._lock:
                for name, definition in previous.items():
                    self._tools[name] = definition
                    self._source_tools.setdefault(source, set()).add(name)
            LOGGER.exception("Feature module %s failed to load; previous tools retained", path.name)
        finally:
            _ACTIVE_REGISTRY.reset(token)

    def load_features(self, feature_dir: str | Path) -> dict[str, str]:
        """Load new or changed .py feature modules without stopping the process.

        The returned mapping contains per-file status messages. A broken file
        does not prevent other feature modules from loading.
        """
        folder = Path(feature_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").touch(exist_ok=True)
        package_name = self._ensure_feature_package(folder)
        results: dict[str, str] = {}
        current: dict[str, tuple[int, int]] = {}

        for path in sorted(folder.glob("*.py")):
            if path.name == "__init__.py" or not path.stem.isidentifier():
                continue
            try:
                signature = self._signature(path)
            except OSError as exc:
                results[path.name] = f"unavailable: {exc}"
                continue
            current[path.name] = signature
            old_signature = self._signatures.get(path.name)
            if old_signature == signature and path.name in self._signatures:
                results[path.name] = "unchanged"
                continue
            self._load_feature(path, package_name)
            results[path.name] = "loaded or reloaded"

        # Unregister deleted feature files and drop their import modules.
        for old_name in set(self._signatures) - set(current):
            old_path = folder / old_name
            self.unregister_source(str(old_path.resolve()))
            module_name = f"{package_name}.{Path(old_name).stem}"
            self._modules.pop(module_name, None)
            self._module_sources.pop(module_name, None)
            sys.modules.pop(module_name, None)
            results[old_name] = "removed"

        self._feature_dir = folder
        self._signatures = current
        return results

    def start_watcher(
        self, feature_dir: str | Path | None = None, *, interval_seconds: float = 1.0
    ) -> None:
        """Start a small standard-library polling watcher. Safe to call repeatedly."""
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
        source = function.__module__
        module = sys.modules.get(function.__module__)
        module_file = getattr(module, "__file__", None)
        if module_file:
            source = str(Path(module_file).resolve())
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

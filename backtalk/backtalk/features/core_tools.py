"""Built-in desktop and memory tools registered with Lumen's registry."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from backtalk.tools import TOOL_DEFINITIONS, execute_tool
from backtalk.registry import tool

_MUTATING = {
    "run_powershell",
    "open_application",
    "open_path",
    "create_file",
    "edit_file",
    "save_memory",
    "append_daily_note",
    "barehands_board_command",
}

_DESCRIPTIONS = {
    "run_powershell": "Run a focused PowerShell command on Windows. Blocked destructive and security-sensitive commands are rejected.",
    "open_application": "Open an installed application from Lumen's application allowlist.",
    "open_path": "Open an existing file or folder inside Lumen's configured workspaces.",
    "create_file": "Create a new UTF-8 text file inside an allowed workspace. Never overwrites an existing file.",
    "edit_file": "Replace an existing UTF-8 text file inside an allowed workspace after creating a timestamped backup.",
}


def _make_handler(tool_name: str) -> Callable[..., Any]:
    def handler(**arguments: Any) -> str:
        return execute_tool(tool_name, arguments)
    handler.__name__ = f"tool_{tool_name}"
    return handler


for _entry in TOOL_DEFINITIONS:
    _function = _entry["function"]
    _name = str(_function["name"])
    _schema = deepcopy(_function["parameters"])
    _schema.setdefault("additionalProperties", False)
    _description = _DESCRIPTIONS.get(
        _name,
        str(_function.get("description") or f"Run Lumen tool {_name}."),
    )
    _description = (
        _description
        .replace(" The user will be asked for approval before it runs.", "")
        .replace(" The user will be asked first.", "")
        .replace(" The user will be asked first", "")
    )
    tool(
        name=_name,
        description=_description,
        params_schema=_schema,
        mutating=_name in _MUTATING,
    )(_make_handler(_name))

del _entry, _function, _name, _schema, _description

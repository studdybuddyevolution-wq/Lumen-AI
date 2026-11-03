"""Local desktop tools for Lumen. Mutating tools are permission-gated in brain.py."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backtalk.config import CFG
from backtalk import memory as _mem

TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "list_directory",
        "description": "List files and folders in an allowed Lumen workspace or memory-vault folder. Read-only.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Folder path"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file in an allowed Lumen workspace or memory-vault folder. Read-only.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_powershell",
        "description": "Run a PowerShell command on Windows. The user will be asked for approval before it runs.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Exact PowerShell command"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "open_application",
        "description": "Open an installed application from Lumen's allowlist. The user will be asked first.",
        "parameters": {"type": "object", "properties": {"app": {"type": "string", "description": "Application name such as Notepad, Calculator, Explorer, Obsidian, VS Code, Chrome, or Edge"}}, "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "open_path",
        "description": "Open a file or folder in Windows, restricted to Lumen's configured workspaces. The user will be asked first.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Existing file or folder path"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "create_file",
        "description": "Create a NEW UTF-8 text file in an allowed workspace. Will not overwrite an existing file. The user will be asked first.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace the contents of an existing UTF-8 text file in an allowed workspace. Creates a timestamped backup first. The user will be asked first.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Search notes and documents in the Obsidian memory vault by keywords. Read-only.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search term or keywords"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_active_priorities",
        "description": "Read the current Active Priorities note from the memory vault. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "Save a new note to the Obsidian memory vault. Automatically checks for sensitive information. Restricted to the vault.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Title of the note"},
            "content": {"type": "string", "description": "Content of the note"},
            "folder": {"type": "string", "description": "Vault folder, e.g. '00 - Inbox', '02 - Marklyf', '03 - Lumen', '04 - Personal', '06 - Resources'"},
            "project": {"type": "string", "description": "Project tag, e.g. 'lumen', 'marklyf', 'personal', 'meta'"},
            "note_type": {"type": "string", "description": "Note type: 'reference', 'plan', 'guide', 'log'"}
        }, "required": ["title", "content"]}}},
    {"type": "function", "function": {
        "name": "append_daily_note",
        "description": "Append a session entry or log to today's daily note in the memory vault. Creates today's note if it does not exist.",
        "parameters": {"type": "object", "properties": {
            "entry_content": {"type": "string", "description": "What got done, decisions made, or notes touched"},
            "topic": {"type": "string", "description": "Session topic or heading"}
        }, "required": ["entry_content"]}}},
    {"type": "function", "function": {
        "name": "barehands_board_command",
        "description": "Send a visual command to the Barehands gesture board on localhost:8794 (e.g. present a card, add an image, or clear the board).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "Action: 'present', 'add_card', 'clear', 'reset'"},
            "title": {"type": "string", "description": "Card title (for present or add_card)"},
            "body": {"type": "string", "description": "Card body text (for present or add_card)"}
        }, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "barehands_board_state",
        "description": "Inspect the current cards and objects on the Barehands gesture board. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
]


def _roots() -> list[Path]:
    values = [CFG.get("agent_dir", "")] + list(CFG.get("extra_dirs") or [])
    roots = []
    for value in values:
        if not value:
            continue
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()
            if p not in roots:
                roots.append(p)
        except (OSError, RuntimeError):
            continue
    return roots


def _safe_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A non-empty path is required.")
    candidate = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not candidate.is_absolute():
        candidate = Path(CFG["agent_dir"]) / candidate
    resolved = candidate.resolve()
    for root in _roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            pass
    raise PermissionError("That path is outside Lumen's configured workspaces.")


def _list_directory(args: dict[str, Any]) -> str:
    folder = _safe_path(args.get("path", ""))
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    items = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:200]
    rows = [f"{'[DIR] ' if p.is_dir() else '[FILE]'}{p.name}" for p in items]
    return "\n".join(rows) if rows else "This folder is empty."


def _read_file(args: dict[str, Any]) -> str:
    path = _safe_path(args.get("path", ""))
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.stat().st_size > 700_000:
        raise ValueError("File is larger than the 700 KB read limit. Narrow the request or use a smaller file.")
    try:
        return path.read_text(encoding="utf-8")[:30000]
    except UnicodeDecodeError as exc:
        raise ValueError("This does not appear to be a UTF-8 text file.") from exc


def _run_powershell(args: dict[str, Any]) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("The PowerShell command is empty.")
    if len(command) > 12000:
        raise ValueError("PowerShell command exceeds the 12,000-character safety limit.")
    blocked = re.compile(
        r"(?i)(\bRemove-Item\b|\bRemove-ItemProperty\b|\b(?:del|erase|rd|rmdir|rm)\b|"
        r"\bFormat-(?:Volume|Partition|Disk)\b|\bClear-Disk\b|\bInitialize-Disk\b|"
        r"\bdiskpart\b|\bStop-Computer\b|\bRestart-Computer\b|\bSet-ExecutionPolicy\b|"
        r"\breg\s+(?:add|delete)\b|\bbcdedit\b|\bClear-RecycleBin\b|\bshutdown\s+/(s|r)\b|"
        r"\b(?:Set|Add|Remove)-MpPreference\b|\b(?:Set|Stop)-Service\b|"
        r"\bsc(?:\.exe)?\s+config\b|\bnet\s+stop\b|\btakeown\b|\bicacls\b.*\/(?:grant|reset)\b|"
        r"\bwinget\s+install\b|\bchoco\s+install\b|\bscoop\s+install\b|"
        r"\bInstall-Module\b|\bAdd-AppxPackage\b|\bmsiexec\s+/(i|package)\b)"
    )
    if blocked.search(command):
        raise PermissionError("This command matches a blocked destructive or security-sensitive operation. Lumen will not run it through the general PowerShell tool.")
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=str(Path(CFG["agent_dir"]).resolve()),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, check=False,
    )
    output = (completed.stdout or "")
    if completed.stderr:
        output += ("\n[stderr]\n" if output else "[stderr]\n") + completed.stderr
    output = output.strip() or "(The command completed with no output.)"
    return f"Exit code: {completed.returncode}\n{output[:10000]}"


_APP_ALLOWLIST = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "obsidian": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Obsidian" / "Obsidian.exe"),
    "vscode": "code.exe",
    "visual studio code": "code.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
}


def _open_application(args: dict[str, Any]) -> str:
    app = str(args.get("app") or "").strip().lower()
    target = _APP_ALLOWLIST.get(app)
    if not target:
        raise ValueError("Application is not on Lumen's allowlist. Supported apps: Notepad, Calculator, File Explorer, Task Manager, Obsidian, VS Code, Chrome, and Edge.")
    executable = target if Path(target).is_file() else shutil.which(target)
    if not executable:
        raise FileNotFoundError(f"Could not find the installed application: {app}")
    subprocess.Popen([executable], close_fds=True)
    return f"Opened {app}."


def _open_path(args: dict[str, Any]) -> str:
    path = _safe_path(args.get("path", ""))
    if not path.exists():
        raise FileNotFoundError(str(path))
    os.startfile(str(path))  # Windows shell association
    return f"Opened {path.name}."


def _write_file(args: dict[str, Any], edit: bool) -> str:
    path = _safe_path(args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("File content must be text.")
    if len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("File content exceeds the 1 MB safety limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if edit:
        if not path.is_file():
            raise FileNotFoundError(f"Cannot edit a file that does not exist: {path}")
        backup = path.with_name(path.name + ".lumen-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        path.write_text(content, encoding="utf-8", newline="")
        return f"Updated {path.name}. Backup saved as {backup.name}."
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {path}. Use edit_file after reviewing it.")
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return f"Created {path.name}."


def _send_barehands_cmd(args: dict[str, Any]) -> str:
    import json
    import urllib.request
    action = str(args.get("action") or "present").strip()
    payload: dict[str, Any] = {"a": action}
    if "title" in args:
        payload["title"] = str(args["title"])
    if "body" in args:
        payload["body"] = str(args["body"])
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8794/cmd", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return f"Command '{action}' successfully sent to Barehands board."
    except Exception as exc:
        return f"Could not reach Barehands board at http://127.0.0.1:8794: {exc}"


def _get_barehands_state() -> str:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8794/state", timeout=3) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return f"Barehands board state: {data[:4000]}"
    except Exception as exc:
        return f"Could not read Barehands board state (port 8794): {exc}"


def execute_tool(name: str, args: dict[str, Any]) -> str:
    if name == "list_directory":
        return _list_directory(args)
    if name == "read_file":
        return _read_file(args)
    if name == "run_powershell":
        return _run_powershell(args)
    if name == "open_application":
        return _open_application(args)
    if name == "open_path":
        return _open_path(args)
    if name == "create_file":
        return _write_file(args, edit=False)
    if name == "edit_file":
        return _write_file(args, edit=True)
    if name == "search_memory":
        return _mem.search_memory(str(args.get("query") or ""))
    if name == "get_active_priorities":
        return _mem.get_active_priorities()
    if name == "save_memory":
        return _mem.save_memory(
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            folder=str(args.get("folder") or "00 - Inbox"),
            project=str(args.get("project") or "personal"),
            note_type=str(args.get("note_type") or "reference"),
        )
    if name == "append_daily_note":
        return _mem.append_daily_note(
            entry_content=str(args.get("entry_content") or ""),
            topic=str(args.get("topic") or "Session"),
        )
    if name == "barehands_board_command":
        return _send_barehands_cmd(args)
    if name == "barehands_board_state":
        return _get_barehands_state()
    raise ValueError(f"Unknown tool: {name}")


def _permission_context(display_name: str, description: str):
    return SimpleNamespace(display_name=display_name, description=description)


def _run_ps_permission(args: dict[str, Any]):
    command = str(args.get("command") or "")
    return "Bash", {"command": command}, _permission_context("PowerShell", "run the exact PowerShell command shown in the approval details")


def _write_permission(args: dict[str, Any]):
    return "Write", {"file_path": str(args.get("path") or "")}, _permission_context("file creation", "create a new file with the proposed content")


def _edit_permission(args: dict[str, Any]):
    return "Edit", {"file_path": str(args.get("path") or "")}, _permission_context("file edit", "replace an existing file after saving a backup")


def _open_app_permission(args: dict[str, Any]):
    return "OpenApplication", {"app": str(args.get("app") or "")}, _permission_context("application launch", f"open {args.get('app', 'an application')} from the built-in allowlist")


def _open_path_permission(args: dict[str, Any]):
    return "OpenPath", {"path": str(args.get("path") or "")}, _permission_context("open a file or folder", "open an existing path inside a configured Lumen workspace")


TOOL_META = {
    "list_directory": {"approval": False},
    "read_file": {"approval": False},
    "run_powershell": {"approval": True, "permission": _run_ps_permission},
    "open_application": {"approval": True, "permission": _open_app_permission},
    "open_path": {"approval": True, "permission": _open_path_permission},
    "create_file": {"approval": True, "permission": _write_permission},
    "edit_file": {"approval": True, "permission": _edit_permission},
    "search_memory": {"approval": False},
    "get_active_priorities": {"approval": False},
    "save_memory": {"approval": False},
    "append_daily_note": {"approval": False},
    "barehands_board_command": {"approval": False},
    "barehands_board_state": {"approval": False},
}
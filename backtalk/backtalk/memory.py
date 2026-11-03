"""Lumen's Obsidian vault memory system.

Provides structured access to the memory vault at the configured path.
All write operations are restricted to the vault directory. Read operations
support searching across note content and frontmatter.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backtalk.config import CFG
from backtalk.vlog import log

# Sensitive patterns that must never be auto-saved
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(password|passwd|pwd)\s*[:=]"
    r"|api[_-]?key\s*[:=]"
    r"|secret[_-]?key\s*[:=]"
    r"|token\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}"
    r"|\b\d{3}-\d{2}-\d{4}\b"  # SSN
    r"|\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"  # credit card
)


def _vault_path() -> Path:
    """Resolve the memory vault path from config."""
    extra = CFG.get("extra_dirs") or []
    for d in extra:
        p = Path(os.path.expandvars(os.path.expanduser(str(d)))).resolve()
        if p.is_dir():
            return p
    raise FileNotFoundError(
        "No memory vault directory found. Check extra_dirs in backtalk.json."
    )


def _ensure_vault_path(target: Path) -> Path:
    """Verify a path is inside the vault. Raises PermissionError otherwise."""
    vault = _vault_path()
    resolved = target.resolve()
    try:
        resolved.relative_to(vault)
    except ValueError:
        raise PermissionError(
            f"Path {resolved} is outside the memory vault at {vault}."
        )
    return resolved


def _has_sensitive_content(text: str) -> bool:
    """Check if text contains patterns that should not be auto-saved."""
    return bool(_SENSITIVE_PATTERNS.search(text))


def search_memory(query: str, max_results: int = 10) -> str:
    """Search vault notes by content and filename.

    Searches note titles and content for the query string (case-insensitive).
    Returns matching note paths and relevant excerpts.
    """
    if not query or not query.strip():
        return "Search query cannot be empty."

    vault = _vault_path()
    query_lower = query.lower().strip()
    results: list[str] = []

    for md_file in vault.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.relative_to(vault).parts):
            continue

        score = 0
        excerpt = ""

        # Check filename match
        if query_lower in md_file.stem.lower():
            score += 10

        # Check content match
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            content_lower = content.lower()
            count = content_lower.count(query_lower)
            if count > 0:
                score += count
                # Extract a relevant excerpt around the first match
                idx = content_lower.find(query_lower)
                start = max(0, idx - 80)
                end = min(len(content), idx + len(query_lower) + 80)
                excerpt = content[start:end].replace("\n", " ").strip()
                if start > 0:
                    excerpt = "..." + excerpt
                if end < len(content):
                    excerpt = excerpt + "..."
        except (OSError, UnicodeDecodeError):
            continue

        if score > 0:
            rel_path = md_file.relative_to(vault)
            entry = f"[{score}] {rel_path}"
            if excerpt:
                entry += f"\n    {excerpt}"
            results.append((score, entry))

    if not results:
        return f"No notes found matching '{query}'."

    results.sort(key=lambda x: x[0], reverse=True)
    output = [r[1] for r in results[:max_results]]
    return f"Found {len(results)} result(s) for '{query}':\n\n" + "\n\n".join(output)


def save_memory(
    title: str,
    content: str,
    folder: str = "00 - Inbox",
    project: str = "personal",
    note_type: str = "reference",
    status: str = "active",
) -> str:
    """Save a new note to the memory vault.

    Creates a new markdown note with proper frontmatter in the specified folder.
    Will not overwrite existing files. Content is checked for sensitive patterns.
    """
    if not title or not title.strip():
        return "Note title cannot be empty."
    if not content or not content.strip():
        return "Note content cannot be empty."

    if _has_sensitive_content(content):
        return (
            "BLOCKED: The content appears to contain sensitive information "
            "(passwords, API keys, card numbers, or government IDs). "
            "Lumen will not auto-save this. Ask Arin for explicit permission "
            "to store sensitive data."
        )

    vault = _vault_path()
    folder_path = vault / folder
    folder_path.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    safe_title = re.sub(r'[<>:"/\\|?*]', "", title.strip())
    if not safe_title:
        return "Invalid note title after sanitization."

    file_path = folder_path / f"{safe_title}.md"
    file_path = _ensure_vault_path(file_path)

    if file_path.exists():
        return (
            f"A note called '{safe_title}' already exists in {folder}. "
            "Use edit or append instead of creating a duplicate."
        )

    frontmatter = (
        f"---\n"
        f"status: {status}\n"
        f"project: {project}\n"
        f"type: {note_type}\n"
        f"created: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"---\n\n"
    )

    full_content = frontmatter + f"# {safe_title}\n\n" + content.strip() + "\n"
    file_path.write_text(full_content, encoding="utf-8", newline="")

    log(f"[memory] saved note: {file_path.relative_to(vault)}")
    return f"Saved note '{safe_title}' to {folder}."


def append_daily_note(entry_content: str, topic: str = "Session") -> str:
    """Append an entry to today's daily note, creating it if needed.

    Daily notes live in `01 - Daily Notes/` with the format YYYY-MM-DD.md.
    If today's note exists, appends a new session section.
    """
    if not entry_content or not entry_content.strip():
        return "Entry content cannot be empty."

    vault = _vault_path()
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    day_name = now.strftime("%A")
    month_name = now.strftime("%B")
    day_num = now.strftime("%d").lstrip("0")
    year = now.strftime("%Y")

    daily_dir = vault / "01 - Daily Notes"
    daily_dir.mkdir(parents=True, exist_ok=True)

    file_path = daily_dir / f"{date_str}.md"
    file_path = _ensure_vault_path(file_path)

    if file_path.exists():
        # Append a new session section
        existing = file_path.read_text(encoding="utf-8", errors="replace")
        # Count existing sessions
        session_count = existing.count("## Session")
        next_session = session_count + 1

        new_section = (
            f"\n\n## Session {next_session} — {time_str}: {topic}\n\n"
            f"{entry_content.strip()}\n"
        )
        file_path.write_text(
            existing + new_section, encoding="utf-8", newline=""
        )
        log(f"[memory] appended session {next_session} to daily note {date_str}")
        return f"Appended session {next_session} to today's daily note."
    else:
        # Create a new daily note
        content = (
            f"---\n"
            f"status: active\n"
            f"project: personal\n"
            f"type: log\n"
            f"created: {date_str}\n"
            f"---\n\n"
            f"# {day_name}, {month_name} {day_num}, {year}\n\n"
            f"## Index\n\n"
            f"- **{topic}** — session entry\n\n"
            f"## Session 1 — {time_str}: {topic}\n\n"
            f"{entry_content.strip()}\n"
        )
        file_path.write_text(content, encoding="utf-8", newline="")
        log(f"[memory] created daily note {date_str}")
        return f"Created today's daily note ({date_str}) with session entry."


def get_active_priorities() -> str:
    """Read the Active Priorities note from the vault root."""
    vault = _vault_path()
    priorities_file = vault / "Active Priorities.md"

    if not priorities_file.is_file():
        return "No Active Priorities note found in the vault."

    try:
        content = priorities_file.read_text(encoding="utf-8", errors="replace")
        return content[:8000]
    except OSError as exc:
        return f"Could not read Active Priorities: {exc}"


def read_vault_index() -> str:
    """Read the VAULT-INDEX.md from the vault root."""
    vault = _vault_path()
    index_file = vault / "VAULT-INDEX.md"

    if not index_file.is_file():
        return "No VAULT-INDEX.md found in the vault."

    try:
        content = index_file.read_text(encoding="utf-8", errors="replace")
        return content[:16000]
    except OSError as exc:
        return f"Could not read VAULT-INDEX: {exc}"

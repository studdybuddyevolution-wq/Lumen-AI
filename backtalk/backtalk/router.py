"""Deterministic relevance routing for small local language models."""
from __future__ import annotations

import re
from collections.abc import Iterable

from backtalk.registry import ToolDefinition

_WORD = re.compile(r"[a-z0-9_]+")

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "do", "for", "from", "how", "i", "in", "is", "it", "me", "my",
    "of", "on", "or", "please", "show", "tell", "that", "the", "this",
    "to", "what", "when", "where", "with", "would", "you", "your",
}

_SYNONYMS = {
    "launch": {"open", "start", "run", "application", "app"},
    "open": {"launch", "start", "application", "app"},
    "notepad": {"editor", "text", "application"},
    "vscode": {"code", "editor", "application"},
    "create": {"make", "write", "new", "file"},
    "write": {"create", "save", "file"},
    "edit": {"change", "modify", "update"},
    "modify": {"edit", "change", "update"},
    "powershell": {"command", "terminal", "shell", "run"},
    "command": {"powershell", "terminal", "shell"},
    "memory": {"vault", "note", "remember", "priorities"},
    "remember": {"memory", "save", "note"},
    "priority": {"priorities", "task", "tasks"},
    "priorities": {"priority", "task", "tasks"},
    "folder": {"directory", "path", "files"},
    "directory": {"folder", "path", "files"},
}


class ToolRouter:
    """Select a focused tool subset using name, description and schema terms."""

    def __init__(self, *, max_tools: int = 8, min_tools: int = 5) -> None:
        if max_tools < 1 or min_tools < 1 or min_tools > max_tools:
            raise ValueError("Tool limits must satisfy 1 <= min_tools <= max_tools.")
        self.max_tools = max_tools
        self.min_tools = min_tools

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token for token in _WORD.findall(value.lower())
            if token not in _STOP_WORDS and len(token) > 1
        }

    def _tool_terms(self, definition: ToolDefinition) -> tuple[set[str], set[str]]:
        name_terms = self._tokens(definition.name.replace("_", " "))
        description_terms = self._tokens(definition.description)
        property_terms = self._tokens(
            " ".join(definition.params_schema.get("properties", {}).keys())
        )
        return name_terms | property_terms, description_terms

    def score(self, query: str, definition: ToolDefinition) -> int:
        query_terms = self._tokens(query)
        expanded = set(query_terms)
        for term in query_terms:
            expanded.update(_SYNONYMS.get(term, set()))
        name_terms, description_terms = self._tool_terms(definition)
        return (
            4 * len(expanded & name_terms)
            + 2 * len(expanded & description_terms)
            + (1 if definition.mutating and any(
                t in expanded for t in {"create", "write", "edit", "run", "save", "open"}
            ) else 0)
        )

    def select(
        self, query: str, available: Iterable[ToolDefinition]
    ) -> list[ToolDefinition]:
        definitions = list(available)
        if not definitions:
            return []
        ranked = sorted(
            definitions,
            key=lambda item: (-self.score(query, item), item.name),
        )
        positive = [item for item in ranked if self.score(query, item) > 0]
        count = min(self.max_tools, max(self.min_tools, len(positive)))
        return ranked[:count]

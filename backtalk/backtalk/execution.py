"""Bounded in-memory execution history for Lumen tool calls.

The tracker is intentionally process-local: it does not write arguments or
results to disk. A small JSON snapshot is retained for the localhost dashboard.
"""
from __future__ import annotations

import json
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from time import perf_counter
from typing import Any
from uuid import uuid4

_TERMINAL_STATUSES = frozenset({"completed", "failed", "denied"})


class ExecutionTracker:
    """Thread-safe, bounded execution ledger shared with the dashboard."""

    def __init__(self, max_events: int = 100, max_payload_chars: int = 20_000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1.")
        if max_payload_chars < 256:
            raise ValueError("max_payload_chars must be at least 256.")
        self._events: deque[dict[str, Any]] = deque()
        self._max_events = max_events
        self._max_payload_chars = max_payload_chars
        self._started_at: dict[str, float] = {}
        self._lock = RLock()

    def _snapshot(self, value: Any) -> Any:
        """Return JSON-safe data, bounding large arguments and results."""
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError, RecursionError):
            encoded = json.dumps(repr(value), ensure_ascii=False)

        if len(encoded) > self._max_payload_chars:
            return {
                "_truncated": True,
                "preview": encoded[: self._max_payload_chars],
                "original_characters": len(encoded),
            }
        try:
            return json.loads(encoded)
        except json.JSONDecodeError:
            return str(value)[: self._max_payload_chars]

    def start(
        self,
        name: str,
        arguments: Any,
        *,
        provider: str = "local",
    ) -> str:
        """Record a running tool call and return its execution ID."""
        execution_id = uuid4().hex
        event = {
            "id": execution_id,
            "name": str(name or "unknown")[:128],
            "args": self._snapshot(arguments),
            "result": None,
            "latency_ms": None,
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "provider": provider if provider in {"local", "cloud"} else "unknown",
        }
        with self._lock:
            if len(self._events) >= self._max_events:
                evicted = self._events.popleft()
                self._started_at.pop(evicted["id"], None)
            self._events.append(event)
            self._started_at[execution_id] = perf_counter()
        return execution_id

    def finish(
        self, execution_id: str, status: str, result: Any
    ) -> dict[str, Any] | None:
        """Set the terminal state and result for a previously started call."""
        if status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(sorted(_TERMINAL_STATUSES))}."
            )
        result_snapshot = self._snapshot(result)
        with self._lock:
            started = self._started_at.pop(execution_id, None)
            for index, current in enumerate(self._events):
                if current["id"] != execution_id:
                    continue
                updated = deepcopy(current)
                updated["status"] = status
                updated["result"] = result_snapshot
                updated["latency_ms"] = (
                    round((perf_counter() - started) * 1000, 2)
                    if started is not None
                    else None
                )
                self._events[index] = updated
                return deepcopy(updated)
        return None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return newest-first event snapshots without exposing mutable state."""
        safe_limit = max(0, min(int(limit), self._max_events))
        with self._lock:
            if not safe_limit:
                return []
            recent_events = list(self._events)[-safe_limit:]
            return [deepcopy(event) for event in reversed(recent_events)]

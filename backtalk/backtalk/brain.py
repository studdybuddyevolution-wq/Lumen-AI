"""Lumen's local Ollama brain with a validated, hot-reloadable tool registry.

The model is shown a relevance-ranked tool subset. Every returned tool call
is validated against its registered JSON Schema before execution.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from pathlib import Path

from backtalk.config import CFG, DISCIPLINE
from backtalk.registry import DEFAULT_REGISTRY, ToolValidationError
from backtalk.execution import ExecutionTracker
from backtalk.features import core_tools as _core_tools  # registers trusted built-ins
from backtalk.router import ToolRouter
from backtalk.vlog import log
from backtalk.selfdev.bug_hunter import BugHunter

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class PermissionResultAllow:
    """Legacy import compatibility; the registry no longer uses action prompts."""
    behavior: str = "allow"


@dataclass
class PermissionResultDeny:
    """Legacy import compatibility; the registry no longer uses action prompts."""
    behavior: str = "deny"
    message: str = "The user denied this action."
    interrupt: bool = False


class WarmBrain:
    """Long-lived chat history backed by a local Ollama model."""

    def __init__(self, model: str | None = None, can_use_tool=None,
                 resume_id: str | None = None):
        self.model = model or CFG.get("model", "qwen3.5:4b")
        # Keep can_use_tool accepted for older launchers; routine actions no
        # longer use an interactive approval callback.
        self._registry = DEFAULT_REGISTRY
        self.execution_tracker = ExecutionTracker(max_events=100)
        self.bug_hunter = BugHunter(Path(__file__).resolve().parents[1], max_reports=100)
        self._router = ToolRouter(max_tools=8, min_tools=5)
        self._feature_dir = Path(__file__).resolve().parent / "features"
        self._features_loaded = False
        self._http: httpx.AsyncClient | None = None
        self._messages: list[dict[str, Any]] = []
        self._system_message: dict[str, str] | None = None
        self._dirty = False
        self.session = {"turns": 0, "out_tokens": 0, "in_tokens": 0,
                        "cost": 0.0}
        self._base_url = str(CFG.get("ollama_url") or
                             "http://127.0.0.1:11434").rstrip("/")
        self._num_ctx = int(CFG.get("ollama_num_ctx") or 8192)
        self._max_tool_rounds = int(CFG.get("ollama_max_tool_rounds") or 6)

    def _load_prompt(self) -> str:
        name = str(CFG.get("name") or "Lumen")
        parts = [
            f"You are {name}, Arin's local desktop AI assistant. "
            "Your brain is a locally running Ollama model. Be helpful, "
            "accurate, practical, and conversational. Address the user as Arin.",
            "You can use the supplied tools to inspect files and perform "
            "ordinary desktop actions requested by the user. Never claim an "
            "action succeeded unless its tool result confirms success.",
            "Ordinary actions such as opening allowlisted applications, opening "
            "workspace paths, creating new files, editing files with backups, "
            "and running permitted PowerShell commands execute without repetitive "
            "approval prompts after the user requests them.",
            "Stay inside configured workspaces and application allowlists. "
            "Never delete user data, format drives, shut down or restart the "
            "computer, alter security settings, bypass Windows UAC, or install "
            "software through general automation. These blocked-operation rules "
            "remain active. Ask before high-impact operations or when a target "
            "is ambiguous.",
            "New Python feature modules and generated auto-heal patches are "
            "different from ordinary desktop actions: they require explicit "
            "human review and approval before they may be installed or applied.",
            "Use the memory vault as a set of user-owned notes. Do not invent "
            "memories or silently overwrite notes. When editing an existing "
            "file, use the edit tool so a backup is created first.",
            "Do not reveal hidden reasoning. Give concise answers by default; "
            "explain technical choices when useful.",
        ]
        agent_dir = str(CFG.get("agent_dir") or "")
        if agent_dir:
            from pathlib import Path
            profile = Path(agent_dir) / "LUMEN.md"
            try:
                if profile.is_file():
                    parts.append("LUMEN PERSONALITY AND OPERATING NOTES:\n" +
                                 profile.read_text(encoding="utf-8")[:24000])
            except OSError as exc:
                log(f"[brain] could not read LUMEN.md: {exc}")
        for folder in CFG.get("extra_dirs") or []:
            try:
                from pathlib import Path
                index = Path(folder) / "VAULT-INDEX.md"
                if index.is_file():
                    parts.append("MEMORY VAULT INDEX:\n" +
                                 index.read_text(encoding="utf-8")[:16000])
                prio = Path(folder) / "Active Priorities.md"
                if prio.is_file():
                    parts.append("ACTIVE PRIORITIES:\n" +
                                 prio.read_text(encoding="utf-8")[:8000])
            except OSError as exc:
                log(f"[brain] could not read vault files: {exc}")
        parts.append(DISCIPLINE)
        return "\n\n".join(parts)

    async def start(self):
        if not self._features_loaded:
            statuses = self._registry.load_features(self._feature_dir)
            self._registry.start_watcher(self._feature_dir)
            self._features_loaded = True
            loaded = sum(1 for status in statuses.values() if status == "loaded")
            log(
                f"[brain] tool registry ready: {len(self._registry.list_tools())} "
                f"tools; {loaded} feature module(s) loaded"
            )
        if self._http is None:
            timeout = httpx.Timeout(180.0, connect=8.0)
            self._http = httpx.AsyncClient(timeout=timeout)
        response = await self._http.get(f"{self._base_url}/api/tags")
        response.raise_for_status()
        models = [m.get("name", "") for m in response.json().get("models", [])]
        if models and self.model not in models and not any(
                m.split(":", 1)[0] == self.model.split(":", 1)[0]
                for m in models):
            raise RuntimeError(
                f"Ollama is running, but model {self.model!r} is not installed. "
                f"Available models: {', '.join(models)}")
        self._system_message = {"role": "system", "content": self._load_prompt()}
        self._messages = [self._system_message.copy()]
        self._dirty = False
        log(f"[brain] connected to local Ollama at {self._base_url}; model={self.model}")

    async def set_permission_mode(self, backtalk_mode: str):
        # Compatibility shim for older dashboard and console clients.
        log("[brain] routine action prompts are disabled; safety checks remain active")

    async def context_usage(self):
        # Ollama's local endpoint does not provide the Claude-style breakdown.
        return None

    async def command(self, cmd: str) -> str:
        text = (cmd or "").strip()
        low = text.lower()
        if low == "/clear":
            self._messages = [self._system_message.copy()] if self._system_message else []
            self.session.update(turns=0, out_tokens=0, in_tokens=0, cost=0.0)
            self._dirty = False
            return "Conversation cleared."
        if low == "/compact":
            if self._system_message:
                recent = self._messages[-16:]
                self._messages = [self._system_message.copy()] + [
                    m for m in recent if m.get("role") != "system"]
            return "Kept the most recent conversation turns."
        if low.startswith("/model "):
            requested = text.split(None, 1)[1].strip()
            if not requested:
                return "No model name was provided."
            self.model = requested
            return f"Model set to {self.model}."
        if low.startswith("/effort "):
            return "Reasoning effort controls are not exposed by this Ollama adapter yet."
        return "That voice-console command is not supported by the local Ollama adapter."

    async def interrupt(self):
        # Cancelling the ask_stream task cancels the active httpx request.
        return None

    async def reset_turn(self, timeout: float = 8.0):
        self._dirty = False

    async def stop(self):
        self._registry.stop_watcher()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _chat(self) -> dict[str, Any]:
        if self._http is None:
            await self.start()
        latest_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(self._messages)
                if message.get("role") == "user"
            ),
            "",
        )
        selected_tools = self._router.select(
            latest_user, self._registry.list_tools()
        )
        payload = {
            "model": self.model,
            "messages": self._messages,
            "tools": self._registry.declarations(
                [item.name for item in selected_tools]
            ),
            "stream": False,
            "think": False,
            "options": {"num_ctx": self._num_ctx},
        }
        response = await self._http.post(f"{self._base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("message"), dict):
            raise RuntimeError(f"Unexpected Ollama response: {str(data)[:500]}")
        self.session["in_tokens"] += int(data.get("prompt_eval_count") or 0)
        self.session["out_tokens"] += int(data.get("eval_count") or 0)
        return data

    @staticmethod
    def _parse_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        raise ValueError("Tool arguments must be a JSON object.")

    async def _run_tool(self, call: dict[str, Any]) -> str:
        """Validate, execute, and record one tool call for the dashboard."""
        fn = call.get("function") or {}
        name = str(fn.get("name") or "unknown")
        raw_arguments = fn.get("arguments", {})

        try:
            args = self._parse_arguments(raw_arguments)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            execution_id = self.execution_tracker.start(
                name, raw_arguments, provider="local"
            )
            message = f"Tool rejected: invalid arguments or schema mismatch ({exc})."
            self.execution_tracker.finish(execution_id, "denied", message)
            return message

        # Current tool handlers run locally. The provider field is retained so
        # the dashboard can distinguish a future explicitly cloud-backed call.
        execution_id = self.execution_tracker.start(
            name, args, provider="local"
        )

        if self._registry.get(name) is None:
            message = f"Tool rejected: {name!r} is not available to Lumen."
            self.execution_tracker.finish(execution_id, "denied", message)
            return message

        try:
            self._registry.validate_arguments(name, args)
        except (TypeError, ValueError, ToolValidationError) as exc:
            message = f"Tool rejected: invalid arguments or schema mismatch ({exc})."
            self.execution_tracker.finish(execution_id, "denied", message)
            return message

        try:
            result = await asyncio.to_thread(self._registry.execute, name, args)
        except asyncio.CancelledError:
            self.execution_tracker.finish(
                execution_id, "failed", "Tool execution was cancelled."
            )
            raise
        except ToolValidationError as exc:
            # A schema may have changed between validation and dispatch.
            message = f"Tool rejected: invalid arguments or schema mismatch ({exc})."
            self.execution_tracker.finish(execution_id, "denied", message)
            return message
        except Exception as exc:
            self.bug_hunter.capture_exception(exc, component=f"tool:{name}")
            message = f"Tool error: {type(exc).__name__}: {exc}"
            log(f"[tools] {name} failed: {exc!r}")
            self.execution_tracker.finish(execution_id, "failed", message)
            return message

        output = str(result)[:12000]
        self.execution_tracker.finish(execution_id, "completed", output)
        return output

    async def ask_stream(self, utterance: str):
        """Run an Ollama conversation, validate and dispatch tools, then speak text."""
        if self._http is None or self._system_message is None:
            await self.start()
        self._messages.append({"role": "user", "content": utterance})
        self._dirty = True
        try:
            for _ in range(self._max_tool_rounds + 1):
                data = await self._chat()
                msg = data["message"]
                calls = msg.get("tool_calls") or []
                self._messages.append(msg)
                content = str(msg.get("content") or "").strip()
                if content:
                    for sentence in _split_sentences(content):
                        yield sentence
                if not calls:
                    self.session["turns"] += 1
                    self._dirty = False
                    if len(self._messages) > 42 and self._system_message:
                        self._messages = [self._system_message.copy()] + self._messages[-32:]
                    return
                for call in calls:
                    result = await self._run_tool(call)
                    fn = call.get("function") or {}
                    tool_name = str(fn.get("name") or "unknown")
                    self._messages.append({
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": result,
                    })
            yield "I reached the tool-call limit for this request. Please split the task into smaller steps."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.bug_hunter.capture_exception(exc, component="ollama_chat")
            log(f"[brain] Ollama request failed: {exc!r}")
            yield "I couldn't complete that request because the local Ollama model returned an error. Check the Backtalk log for details."
        finally:
            self._dirty = False


def _split_sentences(text: str):
    text = " ".join(text.split())
    if not text:
        return
    pieces = _SENTENCE_END.split(text)
    for piece in pieces:
        piece = piece.strip()
        if piece:
            yield piece


if __name__ == "__main__":
    async def demo():
        brain = WarmBrain()
        await brain.start()
        async for sentence in brain.ask_stream("Say hello in one short sentence."):
            print(sentence, flush=True)
        await brain.stop()
    asyncio.run(demo())
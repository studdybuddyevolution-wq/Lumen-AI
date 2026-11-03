"""Lumen Localhost Communication Bridge.

Runs an asyncio HTTP server on localhost:8796 inside the Backtalk voice process.
Allows the Lumen Dashboard to communicate with the single shared WarmBrain instance,
access conversation history and submit typed messages. Routine action approvals are not used.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse, parse_qs

from backtalk.config import CFG
from backtalk.vlog import log

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8796

# Module-level references supplied by main.py
_brain: Any = None
_mouth: Any = None
_perm_dict: dict[str, Any] | None = None
_server: asyncio.Server | None = None
_chat_handler: Callable[[str], Coroutine[Any, Any, str]] | None = None
_interrupt_handler: Callable[[], Coroutine[Any, Any, None]] | None = None


def _capture_bug(exc: BaseException, component: str) -> None:
    """Send an unexpected runtime failure to the brain's bounded Bug Hunter."""
    hunter = getattr(_brain, "bug_hunter", None) if _brain is not None else None
    if hunter is None:
        return
    try:
        hunter.capture_exception(exc, component=component)
    except Exception:
        # Diagnostics must never make a user-facing request fail.
        pass


def setup_bridge(
    brain: Any,
    mouth: Any,
    perm_dict: dict[str, Any],
    chat_handler: Callable[[str], Coroutine[Any, Any, str]],
    interrupt_handler: Callable[[], Coroutine[Any, Any, None]],
):
    """Register runtime objects with the bridge."""
    global _brain, _mouth, _perm_dict, _chat_handler, _interrupt_handler
    _brain = brain
    _mouth = mouth
    _perm_dict = perm_dict
    _chat_handler = chat_handler
    _interrupt_handler = interrupt_handler


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle one incoming HTTP connection."""
    try:
        line = await reader.readline()
        if not line:
            writer.close()
            return
        req_line = line.decode("utf-8", errors="replace").strip()
        parts = req_line.split()
        if len(parts) < 2:
            writer.close()
            return
        method, path = parts[0], parts[1]

        # Read headers
        headers: dict[str, str] = {}
        while True:
            hline = await reader.readline()
            if not hline or hline in (b"\r\n", b"\n"):
                break
            hstr = hline.decode("utf-8", errors="replace").strip()
            if ":" in hstr:
                k, v = hstr.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # Read body if present
        content_len = int(headers.get("content-length", 0))
        body = b""
        if content_len > 0:
            body = await reader.readexactly(content_len)

        parsed_url = urlparse(path)
        route = parsed_url.path

        # Dispatch routes
        resp_status = 200
        resp_data: dict[str, Any] = {}

        if method == "OPTIONS":
            await _send_response(writer, 204, None)
            return

        if method == "GET" and route == "/bridge/status":
            resp_data = _get_status()
        elif method == "GET" and route == "/bridge/history":
            resp_data = _get_history()
        elif method == "GET" and route == "/bridge/executions":
            params = parse_qs(parsed_url.query)
            try:
                limit = int(params.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            resp_data = _get_executions(limit)
        elif method == "GET" and route == "/bridge/bugs":
            params = parse_qs(parsed_url.query)
            try:
                limit = int(params.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            resp_data = _get_bugs(limit)
        elif method == "GET" and route == "/bridge/approval":
            resp_data = _get_approval()
        elif method == "POST" and route == "/bridge/approval":
            resp_data = _handle_approval(body)
        elif method == "POST" and route == "/bridge/chat":
            if _chat_handler is None:
                resp_status = 503
                resp_data = {"error": "Chat handler not ready"}
            else:
                try:
                    payload = json.loads(body.decode("utf-8") if body else "{}")
                    message = str(payload.get("message") or "").strip()
                    if not message:
                        resp_status = 400
                        resp_data = {"error": "Message cannot be empty"}
                    else:
                        reply = await _chat_handler(message)
                        resp_data = {"response": reply}
                except Exception as exc:
                    _capture_bug(exc, "bridge_chat")
                    log(f"[bridge] error handling chat: {exc}")
                    resp_status = 500
                    resp_data = {"error": str(exc)}
        elif method == "POST" and route == "/bridge/clear":
            if _brain is not None:
                await _brain.command("/clear")
                resp_data = {"status": "cleared"}
            else:
                resp_status = 503
                resp_data = {"error": "Brain not ready"}
        elif method == "POST" and route == "/bridge/interrupt":
            if _interrupt_handler is not None:
                await _interrupt_handler()
                resp_data = {"status": "interrupted"}
            else:
                resp_data = {"status": "no interrupt handler"}
        else:
            resp_status = 404
            resp_data = {"error": f"Not found: {method} {route}"}

        await _send_response(writer, resp_status, resp_data)

    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as exc:
        _capture_bug(exc, "bridge_request")
        log(f"[bridge] client handler error: {exc}")
        try:
            await _send_response(writer, 500, {"error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _send_response(writer: asyncio.StreamWriter, status: int, data: dict[str, Any] | None):
    """Serialize and write HTTP response with CORS headers."""
    reason = "OK" if status == 200 else ("No Content" if status == 204 else "Error")
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else b""

    headers = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body_bytes)}",
        "Access-Control-Allow-Origin: *",
        "Access-Control-Allow-Methods: GET, POST, OPTIONS",
        "Access-Control-Allow-Headers: Content-Type",
        "Connection: close",
        "",
        "",
    ]
    raw = "\r\n".join(headers).encode("utf-8") + body_bytes
    writer.write(raw)
    try:
        await writer.drain()
    except Exception:
        pass


def _get_status() -> dict[str, Any]:
    """Return live runtime and tool-registry status."""
    speaking = _mouth.speaking if _mouth is not None else False
    model_name = getattr(_brain, "model", CFG.get("model", "qwen3.5:4b"))
    turns = _brain.session.get("turns", 0) if _brain is not None else 0
    registry = getattr(_brain, "_registry", None) if _brain is not None else None
    tools = registry.list_tools() if registry is not None else []
    feature_sources = {
        item.source.replace("\\", "/")
        for item in tools
        if item.source != "builtin"
        and not item.source.replace("\\", "/").endswith("/features/core_tools.py")
    }

    return {
        "status": "online",
        "runtime": "backtalk",
        "model": model_name,
        "name": CFG.get("name", "Lumen"),
        "speaking": speaking,
        "turns": turns,
        "execution_mode": "autonomous",
        "registered_tools": len(tools),
        "feature_modules": len(feature_sources),
        "pending_approval": False,
    }


def _get_history() -> dict[str, Any]:
    """Return conversation history from WarmBrain."""
    if _brain is None:
        return {"messages": []}

    formatted = []
    for msg in getattr(_brain, "_messages", []):
        role = msg.get("role")
        if role == "system":
            continue
        content = msg.get("content") or ""
        if not content and msg.get("tool_calls"):
            # Summarize tool calls
            calls = msg.get("tool_calls", [])
            names = [c.get("function", {}).get("name", "tool") for c in calls]
            content = f"[Tool call: {', '.join(names)}]"
        if content:
            formatted.append({
                "role": role,
                "content": content,
            })
    return {"messages": formatted}


def _get_executions(limit: int = 50) -> dict[str, Any]:
    """Return recent bounded tool-execution events for the dashboard."""
    tracker = (
        getattr(_brain, "execution_tracker", None)
        if _brain is not None
        else None
    )
    if tracker is None:
        return {
            "events": [],
            "count": 0,
            "counts": {"running": 0, "completed": 0, "failed": 0, "denied": 0},
        }

    events = tracker.recent(max(1, min(int(limit), 100)))
    counts = {status: 0 for status in ("running", "completed", "failed", "denied")}
    for event in events:
        status = event.get("status")
        if status in counts:
            counts[status] += 1
    return {"events": events, "count": len(events), "counts": counts}


def _get_bugs(limit: int = 50) -> dict[str, Any]:
    """Return bounded, sanitised runtime bug reports for the localhost dashboard."""
    hunter = getattr(_brain, "bug_hunter", None) if _brain is not None else None
    if hunter is None:
        return {
            "events_seen": 0,
            "unique_reports": 0,
            "recurrent_reports": 0,
            "reports": [],
        }
    try:
        return hunter.summary(max(0, min(int(limit), 100)))
    except (TypeError, ValueError):
        return hunter.summary(50)


def _get_approval() -> dict[str, Any]:
    """Legacy endpoint retained for compatibility; no routine approvals exist."""
    return {"pending": None, "enabled": False}


def _handle_approval(body: bytes) -> dict[str, Any]:
    """Reject legacy dashboard approval submissions without changing state."""
    return {
        "status": "disabled",
        "message": "Routine desktop-action approvals are disabled.",
    }


async def start_bridge_server():
    """Start the bridge HTTP server."""
    global _server
    if _server is not None:
        return
    try:
        _server = await asyncio.start_server(_handle_client, BRIDGE_HOST, BRIDGE_PORT)
        log(f"[bridge] communication bridge listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}")
    except OSError as exc:
        log(f"[bridge] failed to start bridge server on port {BRIDGE_PORT}: {exc}")


async def stop_bridge_server():
    """Stop the bridge server."""
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
        log("[bridge] communication bridge stopped")

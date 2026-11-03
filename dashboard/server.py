"""Lumen Dashboard — lightweight local web server.

Serves the dashboard UI and bridges typed chat directly
to the WarmBrain runtime inside the Backtalk process on localhost:8796.

Architecture:
- The Backtalk process is the PRIMARY runtime (owns WarmBrain and all tools).
- This dashboard connects to Backtalk's bridge on port 8796.
- Both voice and typed chat share the exact same WarmBrain instance and history.
- Routine desktop-action approvals are disabled; runtime safety checks remain active.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DASHBOARD_PORT = 8795
BRIDGE_URL = "http://127.0.0.1:8796"
BAREHANDS_PORT = 8794
OLLAMA_URL = "http://127.0.0.1:11434"

LUMEN_ROOT = Path(__file__).resolve().parent.parent
BACKTALK_ROOT = LUMEN_ROOT / "backtalk"


def check_port(host: str, port: int, timeout: float = 0.08) -> bool:
    """Check if a service is listening on the given port quickly."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, TimeoutError, OSError):
        return False


def get_vault_path() -> Path | None:
    """Resolve vault path from backtalk.json."""
    try:
        cfg_file = BACKTALK_ROOT / "backtalk.json"
        if cfg_file.is_file():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            for d in cfg.get("extra_dirs", []):
                p = Path(os.path.expandvars(os.path.expanduser(str(d)))).resolve()
                if p.is_dir() and (p / "VAULT-INDEX.md").is_file():
                    return p
    except Exception:
        pass
    default = Path("C:/Users/ARIN/Lumen Memory")
    return default if default.is_dir() else None


def get_status() -> dict:
    """Gather live verified status of all Lumen components."""
    # 1. Ollama status
    ollama_ok = False
    models = []
    if check_port("127.0.0.1", 11434, timeout=0.08):
        try:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                ollama_ok = True
        except Exception:
            pass

    # 2. Bridge / Voice runtime status
    bridge_ok = False
    bridge_data = {}
    if check_port("127.0.0.1", 8796, timeout=0.08):
        try:
            req = urllib.request.Request(f"{BRIDGE_URL}/bridge/status")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                bridge_data = json.loads(resp.read().decode("utf-8"))
                bridge_ok = True
        except Exception:
            pass

    # 3. Voice instance lock port (8791)
    voice_running = check_port("127.0.0.1", 8791, timeout=0.08) or bridge_ok

    # 4. Barehands port (8794)
    barehands_running = check_port("127.0.0.1", BAREHANDS_PORT, timeout=0.08)

    # 5. Signal file for voice state
    voice_state = "offline"
    try:
        state_file = BACKTALK_ROOT / ".voice_state"
        if state_file.is_file():
            voice_state = state_file.read_text(encoding="utf-8").strip() or "idle"
    except Exception:
        pass

    vault = get_vault_path()

    return {
        "ollama": "connected" if ollama_ok else "offline",
        "models": models,
        "active_model": bridge_data.get("model", "qwen3.5:4b"),
        "voice_process": "running" if voice_running else "stopped",
        "voice": voice_state if voice_running else "offline",
        "bridge": "connected" if bridge_ok else "disconnected",
        "whisper": "loaded" if voice_running else "not loaded",
        "kokoro": "loaded" if voice_running else "not loaded",
        "memory": str(vault) if vault else "not configured",
        "barehands": "running" if barehands_running else "stopped",
        "execution_mode": bridge_data.get("execution_mode", "autonomous"),
        "registered_tools": bridge_data.get("registered_tools", 0),
        "feature_modules": bridge_data.get("feature_modules", 0),
        "turns": bridge_data.get("turns", 0),
        "pending_approval": False,
    }


def bridge_request(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    """Forward a request to the Backtalk bridge on port 8796."""
    url = f"{BRIDGE_URL}{path}"
    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            res_body = resp.read().decode("utf-8")
            return resp.status, json.loads(res_body) if res_body else {}
    except urllib.error.HTTPError as exc:
        try:
            err_data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            err_data = {"error": f"HTTP {exc.code}: {exc.reason}"}
        return exc.code, err_data
    except Exception as exc:
        return 503, {
            "error": "Lumen voice runtime is offline. Start Lumen from start-lumen.bat to begin.",
            "detail": str(exc),
        }


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serve static dashboard files and proxy API requests."""

    def __init__(self, *args, **kwargs):
        self._static_dir = str(Path(__file__).parent)
        super().__init__(*args, directory=self._static_dir, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/status":
            self._json_response(get_status())
            return

        if route == "/api/messages":
            status_code, data = bridge_request("/bridge/history")
            self._json_response(data, status_code)
            return

        if route == "/api/approval":
            status_code, data = bridge_request("/bridge/approval")
            self._json_response(data, status_code)
            return

        if route == "/api/memory/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            results = self._search_memory(query)
            self._json_response({"results": results})
            return

        if route == "/api/memory/priorities":
            content = self._get_priorities()
            self._json_response({"content": content})
            return

        # Serve static assets
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if route == "/api/chat":
            message = str(payload.get("message") or "").strip()
            if not message:
                self._json_response({"error": "Empty message"}, 400)
                return
            status_code, data = bridge_request("/bridge/chat", method="POST", payload={"message": message})
            self._json_response(data, status_code)
            return

        if route == "/api/approval/respond":
            status_code, data = bridge_request("/bridge/approval", method="POST", payload=payload)
            self._json_response(data, status_code)
            return

        if route == "/api/clear":
            status_code, data = bridge_request("/bridge/clear", method="POST")
            self._json_response(data, status_code)
            return

        if route == "/api/interrupt":
            status_code, data = bridge_request("/bridge/interrupt", method="POST")
            self._json_response(data, status_code)
            return

        if route == "/api/open-obsidian":
            res = self._open_obsidian()
            self._json_response(res)
            return

        self._json_response({"error": f"Unknown route: {route}"}, 404)

    def _search_memory(self, query: str) -> list[dict]:
        """Search notes in the Obsidian memory vault."""
        vault = get_vault_path()
        if not vault or not query.strip():
            return []

        q = query.lower().strip()
        matches = []
        for md_file in vault.rglob("*.md"):
            if any(p.startswith(".") for p in md_file.relative_to(vault).parts):
                continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                if q in md_file.stem.lower() or q in content.lower():
                    # Extract snippet
                    idx = content.lower().find(q)
                    snippet = ""
                    if idx >= 0:
                        start = max(0, idx - 60)
                        end = min(len(content), idx + len(q) + 80)
                        snippet = content[start:end].replace("\n", " ").strip()
                    matches.append({
                        "path": str(md_file.relative_to(vault)).replace("\\", "/"),
                        "title": md_file.stem,
                        "excerpt": snippet or content[:150].replace("\n", " ").strip(),
                    })
                    if len(matches) >= 25:
                        break
            except Exception:
                continue
        return matches

    def _get_priorities(self) -> str:
        """Fetch content of Active Priorities.md."""
        vault = get_vault_path()
        if not vault:
            return "No memory vault configured."
        prio_file = vault / "Active Priorities.md"
        if not prio_file.is_file():
            return "Active Priorities.md note not found."
        try:
            return prio_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading priorities: {exc}"

    def _open_obsidian(self) -> dict:
        """Attempt to launch Obsidian."""
        local_app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Obsidian" / "Obsidian.exe"
        try:
            if local_app.is_file():
                subprocess.Popen([str(local_app)], close_fds=True)
                return {"status": "ok", "message": "Obsidian opened"}
            else:
                subprocess.Popen(["cmd.exe", "/c", "start", "obsidian:"], close_fds=True)
                return {"status": "ok", "message": "Obsidian URL protocol invoked"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), DashboardHandler)
    print(f"[dashboard] Lumen Dashboard running at http://127.0.0.1:{DASHBOARD_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
        server.shutdown()


if __name__ == "__main__":
    main()

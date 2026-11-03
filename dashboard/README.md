# Lumen Dashboard

Local web dashboard for the Lumen AI assistant.

## Quick Start

```
cd C:\Users\ARIN\Lumen\dashboard
python server.py
```

Then open http://127.0.0.1:8795 in your browser.

## Features

- **Home** — System status for all Lumen components
- **Chat** — Typed conversation with Lumen (uses Ollama directly)
- **Execution Mode** — View autonomous execution state and registered tool/feature counts
- **Memory** — Search and browse the Obsidian memory vault
- **Desktop** — Launch applications and run commands
- **Gestures** — Barehands gesture control and tutorials

## Architecture

- Pure Python HTTP server, no external dependencies
- Communicates with Ollama directly for chat
- Reads Backtalk config for memory vault path
- Bound to localhost only — never exposed to the network

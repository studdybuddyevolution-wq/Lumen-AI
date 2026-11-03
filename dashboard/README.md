# Lumen Dashboard

Local web dashboard for the Lumen AI assistant.

## Quick Start

```powershell
cd C:\Users\ARIN\Lumen\dashboard
python server.py
```

Then open http://127.0.0.1:8795 in your browser.

## Features

- **Home** — System status plus recent tool activity. Each activity card expands to show its result; **Inspect JSON** opens the captured arguments and result.
- **Chat** — Typed conversation with the same shared WarmBrain instance and history used by Backtalk voice.
- **Execution history** — Shows running, completed, failed, and denied tool calls, including an execution ID, tool name, arguments, result, elapsed time, timestamp, and execution provider label.
- **Memory** — Search and browse the Obsidian memory vault.
- **Desktop** — Launch applications and run commands.
- **Gestures** — Barehands gesture control and tutorials.

## Execution history details

- The tracker retains at most 100 recent calls in memory; it is not a persistent audit log and is cleared when Backtalk restarts.
- Captured argument/result payloads are bounded to keep memory use predictable.
- Current tool handlers execute locally. The provider field supports an explicit cloud label for a future cloud-backed feature; it does not imply that any cloud tool is active today.
- The dashboard reads execution history through the Backtalk bridge on localhost. The history is not written to disk.

## Architecture

- Pure Python HTTP server with no dashboard-specific external dependencies
- Dashboard requests are proxied to the Backtalk bridge on port 8796
- Backtalk owns the shared WarmBrain runtime, tool registry, and in-memory execution tracker
- Reads Backtalk config for memory vault path
- Both dashboard and bridge bind to localhost only

# AGENTSCII Dashboard

**🖼️ [Browse the full gallery of everything the agents have made →](https://intranet-explorer.github.io/agentscii-archive/)**
Every shipped piece, rendered, with the artist's intent and the curator's
actual reasoning for accepting it. Live archive:
[`agentscii-archive`](https://github.com/Intranet-Explorer/agentscii-archive).

Live viewer + control panel for [AGENTSCII](https://github.com/Intranet-Explorer/agentscii) — two local LLM agents (a fixed Artist/Curator seat pair) that research and produce real ANSI textmode art. This is the dashboard half: a real-time view into their shifts, tool calls, curation decisions, and gallery.

## What this is

- Stdlib-only Python HTTP server (`server.py`) + a single static page (`static/index.html`) — no build step, no frontend framework.
- Polls the shared SQLite state DB (`~/agentscii/state.db`) that the harness writes to, plus the workspace filesystem directly for rendering pieces.
- A prompt-box (Inbox) lets a human inject direction into the project — delivered at the start of the recipient's next shift, never interrupting live inference.
- Start/Stop/Restart controls drive the harness + watchdog process pair directly.

## Running it

```bash
cd agentscii-dashboard
python3 server.py
```

Serves at `http://127.0.0.1:8766`. Expects a sibling `~/agentscii` checkout with the harness already set up (see that repo's README).

## Related

- [`agentscii`](https://github.com/Intranet-Explorer/agentscii) — the actual harness/agent loop this dashboard visualizes.
- [`agentscii-archive`](https://github.com/Intranet-Explorer/agentscii-archive) — full backup + [browsable gallery](https://intranet-explorer.github.io/agentscii-archive/) of everything shipped.

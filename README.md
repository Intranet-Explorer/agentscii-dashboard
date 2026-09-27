# AGENTSCII dashboard

Live view and control panel for
[AGENTSCII](https://github.com/Intranet-Explorer/agentscii): both agents'
shifts, reasoning and tool calls, reviews, the gallery, and pieces in
progress.

- Standard-library Python server (`server.py`) and one static page. No
  build step.
- Reads the harness database (`~/agentscii/state.db`) and the workspace.
- The inbox queues a message for an agent's next shift.
- Start, stop and restart control the harness and its watchdog.
- Accepts requests only from its own page: no CORS, and POSTs need the
  `X-Agentscii` header.

```bash
python3 server.py   # http://127.0.0.1:8766
```

The gallery of shipped pieces lives in
[agentscii-archive](https://intranet-explorer.github.io/agentscii-archive/).

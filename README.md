# MetaMcp

An HTTP multiplexer that serves several MCP servers (stdio or HTTP) behind a single endpoint.

## The problem

Claude Code starts one stdio process per MCP server, per session. With five sessions open and
five servers configured, that is 25 processes doing the same work, each holding its own memory,
its own connections and its own login. MetaMcp runs each backend **once** and lets every session
share it over HTTP.

```
                         ┌───────────────────────────────────────────────┐
                         │                MetaMcp (:8750)                │
Claude Code #1 ─── HTTP ─┤  /servers/notes/mcp     ──► [stdio] python    │
Claude Code #2 ─── HTTP ─┤  /servers/calendar/mcp  ──► [stdio] node      │
Claude Code #3 ─── HTTP ─┤  /servers/search/mcp    ──► [http]  python    │
                         └───────────────────────────────────────────────┘
```

The same pattern as an API gateway in front of internal services: one entry point, one place for
logging, filtering and recovery, and backends that can change without touching the clients.

## How it works

1. **Startup**: reads `servers.json`, expands environment variables (`$VAR` / `${VAR}`, so
   secrets stay out of the file), and creates one proxy per backend with FastMCP's
   `create_proxy()`: a spawned process for `command` entries, a forwarded URL for `url` entries.
2. **Mounting**: each proxy is mounted at `/servers/<name>/mcp` on a single Starlette app.
3. **Auto-recovery**: a crashed stdio backend is respawned with exponential backoff
   (1 s, 2 s, 5 s, 10 s, 30 s), up to 5 attempts. HTTP backends that are not up yet at boot are
   retried the same way.
4. **Hot reload**: when `servers.json` changes, MetaMcp diffs it against the running backends:
   stops removed ones, starts new ones, restarts changed ones. An invalid file is logged and
   ignored; the running state is kept.
5. **Sessions**: inactive sessions are closed after 30 minutes. After a restart, clients holding
   an old session id get a 404 and re-initialise, as the MCP protocol expects.
6. **Tool governance**: per backend, `exposedTools` (allow list) or `hiddenTools` (deny list)
   control what clients can see, and `toolOverrides` rename or re-describe tools; calls are
   mapped back to the original names transparently.
7. **Observability**: every tool call is logged with its duration, slow calls (> 5 s) are
   flagged, and a standalone watchdog checks health from outside the process.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | global status and every backend's status |
| `GET` | `/servers/<name>/health` | one backend's status |
| `POST` / `GET` / `DELETE` | `/servers/<name>/mcp` | MCP protocol (Streamable HTTP) |
| `GET` | `/servers/<name>/tools` | list tools, no MCP session needed |
| `POST` | `/servers/<name>/call` | call a tool, no MCP session needed |
| `POST` | `/servers/<name>/restart` | restart one backend |
| `POST` | `/restart` | restart all backends in parallel |

```bash
curl -X POST http://127.0.0.1:8750/servers/notes/call \
  -H "Content-Type: application/json" \
  -d '{"tool": "search_pages", "args": {"query": "roadmap"}}'
```

## Configuration

Copy `servers.example.json` to `servers.json` (git-ignored) and declare the backends:

```json
{
  "port": 8750,
  "servers": {
    "notes":  { "command": "python", "args": ["path/to/server.py"],
                "env": { "API_KEY": "${NOTES_API_KEY}" }, "hiddenTools": ["debug_internal"] },
    "search": { "url": "http://127.0.0.1:8751/mcp" }
  }
}
```

Then point each Claude Code session at `http://127.0.0.1:8750/servers/<name>/mcp`.

## Run

```bash
uv sync
uv run python -m metamcp                 # the server
uv run python -m metamcp.watchdog        # one health check
uv run python -m metamcp.watchdog --status
```

`python -m metamcp.watchdog --install` registers the watchdog as a Windows scheduled task.

## Tests

```bash
uv sync --extra test
uv run pytest -q                         # 55 tests
```

Python 3.11+, FastMCP 3, Starlette / Uvicorn.

## License

MIT, see [`LICENSE`](LICENSE).

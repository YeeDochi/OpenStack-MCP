# OpenStack-MCP

**English** | [한국어](README.ko.md)

A Model Context Protocol (MCP) server for OpenStack — 123 tools across 7 domains, built on [openstacksdk](https://docs.openstack.org/openstacksdk/), with stateless per-caller header auth, a declarative RESOURCES registry, and Kolla log observability.

---

## Architecture

```
LLM client (Claude / any MCP host)
        │  MCP protocol (stdio or HTTP/SSE)
        ▼
┌─────────────────────────────────────────────────────┐
│              src/core/  (package)                   │
│                                                     │
│  server.py     → CORE_SPECS table → auto-generated  │
│                  list/show/update/delete tools      │
│                  + hand-written specials            │
│  registry.py   → domain/tier tool registry          │
│  assembly.py   → per-domain FastMCP instances,      │
│                  ASGI app, shared conventions text  │
│                                                     │
│  context.os_conn(ctx) ──► os_backend.py            │
│  (per-caller creds)        openstacksdk Connection  │
│                            (Keystone app credential)│
│                                ▼                    │
│                       OpenStack APIs                │
│                       Nova · Neutron · Cinder       │
│                       Glance · Keystone · Octavia   │
│                       Placement                     │
│                                                     │
│  observability.py ──► Kolla log files (read-only)  │
│                          /var/log/kolla/*           │
└─────────────────────────────────────────────────────┘

Per-domain HTTP mounts (stateful sessions for elicitation):
  /compute/mcp   /network/mcp   /lbaas/mcp
  /storage/mcp   /image/mcp     /identity/mcp
  /observability/mcp
```

Each domain is an independent FastMCP instance. A shared process exposes all mounts; `MCP_DOMAINS` and `MCP_TIERS` narrow which tools are active.

---

## Features

- **Declarative registry** — `CORE_SPECS` table + `make_list/make_show/make_update/make_delete` generators; adding a new resource is one dict entry.
- **Stateless per-caller auth** — credentials are read from request headers on every call (HTTP) or from env vars (stdio). The server stores nothing; multiple callers with different credentials share one process safely.
- **Structured error envelope** — all tool errors surface as `Error executing tool <name>: {"error":{"type","message","http_status?}}`. Parse from the first `{`.
- **Delete confirmation** — `*_delete` tools use MCP elicitation to require an explicit human `"delete"` choice before executing. Irreversible operations cannot be triggered by an LLM alone.
- **Key-columns / detail** — list tools return a compact key-column view by default; pass `detail=True` for all fields. `limit=N` caps row count. `all_projects=True` for the admin view where supported.
- **Multimount** — 7 per-domain FastMCP instances served at `/<domain>/mcp`, each carrying a routing map in its `initialize` instructions so clients pick the right mount on the first try.
- **Kolla log observability** — `log_targets`, `log_tail`, `log_trace` read Kolla service log files directly from the host filesystem (mounted read-only), with time-window filtering, regex grep, and request-ID cross-service tracing.

---

## Documentation

- **[Usage](docs/USAGE.md)** — install, stdio & HTTP modes, container, and configuration reference.
- **[Tool Reference](docs/TOOLS.md)** — all 123 tools by domain.

Quick install:

```bash
git clone https://github.com/YeeDochi/OpenStack-MCP.git
cd OpenStack-MCP
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then see [Usage](docs/USAGE.md) to run in stdio or HTTP mode.

---

## Extending: add a new resource

Create tools is intentionally not implemented — it is the primary extension point. To add a create tool or a new resource type:

1. Add a function in `src/core/server.py` or `src/core/os_backend.py` using openstacksdk.
2. Register it with `reg.add(fn, name="...", domain="...", tier="write")`.
3. For a full CRUD resource, add one dict to `CORE_SPECS` (in `src/core/specs.py`); `register_resources` in `registry.py` generates the list/show/update/delete tools automatically from `os_list`/`os_show`/`os_update`/`os_delete`/`update_fields`.

Any OpenStack service supported by openstacksdk can be wired in this way with a handful of lines.

---

## Running tests

```bash
pytest -q
```

The test suite covers registry assembly (tool set, domains, tiers), the list/show/update/delete factories, the delete-confirmation elicitation flow, and the Kolla log backend (target resolution, time-window tailing, request-ID extraction).

---

## License

MIT — see [LICENSE](LICENSE).

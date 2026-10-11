"""Multimount assembly: registry -> per-domain FastMCP instances + ASGI app.
Clean of any management-layer symbols; the management layer injects its own
error detail / branding via the wrap= and brand= parameters."""
from __future__ import annotations

import functools
import inspect
import json
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import UrlElicitationRequiredError

from core.registry import DOMAINS, TIERS

MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
_HOST_NAMES = [h.strip() for h in os.environ.get(
    "MCP_ALLOWED_HOST_NAMES", "127.0.0.1,localhost,127.0.0.1").split(",") if h.strip()]
_DEFAULT_HOSTS = ",".join(f"{h}:{MCP_PORT}" for h in _HOST_NAMES)
_HOSTS = os.environ.get("MCP_ALLOWED_HOSTS", _DEFAULT_HOSTS)
_TS = TransportSecuritySettings(
    allowed_hosts=[h.strip() for h in _HOSTS.split(",") if h.strip()], allowed_origins=["*"])

DOMAIN_GIST = {
    "compute": "servers, flavors, keypairs, hypervisors, availability zones",
    "network": "networks, subnets, routers, ports, security groups, floating IPs, agents, RBAC, IP availability",
    "storage": "block volumes, snapshots, backups, volume types, volume groups, group snapshots, volume services",
    "lbaas": "load balancers, listeners, pools, health monitors, L7 policies, load balancer flavors",
    "image": "images, metadata definition namespaces",
    "identity": "projects, domains, roles, users, role assignments, application credentials, regions, services, endpoints",
    "observability": "kolla host logs (log_tail/log_targets — node-local; multi-node via node=: "
                      "log_targets(node='') lists registered nodes, then log_tail(target, node=<name>) "
                      "proxies to that node), log_trace (request-id journeys), service_status "
                      "(Nova compute services + Neutron network agents up/down)",
}

# SCOPE line — built per-mount from the tiers it actually serves. Write-tier mounts
# say so; read-only slices say so; the host boundary (this MCP manages resources via
# the API, it cannot touch host/control-plane) stays in both. Ported from the retired
# monolith's _scope_line (that version is neutral apart from one dual-backend mention
# stripped below: "agent_mode" gating does not exist on the OpenStack-only edition —
# core's only elicitation gate is the human confirm on delete).
def scope_line(tiers):
    writable = bool({"write", "maintain"} & set(tiers))
    if writable:
        return (
            "⚠️ SCOPE — this MCP does full API-level CRUD on OpenStack resources: "
            "create/update/delete across domains, plus server/volume/router actions "
            "(start/stop/...). delete asks for a human confirmation (confirm/cancel) "
            "before it runs — irreversible. What it CANNOT do is host/control-plane "
            "ops — restarting kolla services (cinder-backup, rabbit, galera), "
            "evacuating hypervisors, host networking — those need direct host/SSH. "
            "Use this MCP to manage resources AND diagnose; do control-plane fixes "
            "on the host."
        )
    return (
        "⚠️ SCOPE — this mount is READ-ONLY (diagnose/inspect only). No state changes "
        "here. Resource CRUD lives on the write-tier mounts; host/control-plane fixes "
        "(restarting kolla services, evacuating hypervisors) need direct host/SSH. "
        "Use this MCP to DIAGNOSE; do the fix elsewhere."
    )


# Conventions common to every core (single, OpenStack-only backend) mount — the
# single-backend subset of the retired monolith's shared conventions block.
# Deliberately strips everything that only makes sense once a second backend
# enters the picture: primary/fallback routing, backend_reason value variety,
# cross-mount session-sharing notes, and the verbatim-values-so-casing/timestamps
# -differ-by-backend caveat. A richer management layer built on core supplies its
# own, fuller, conventions block via the conventions= param below.
CONVENTIONS = (
    "Conventions shared by every mount:\n"
    "- Tool names are regular: `<resource>_list` / `<resource>_show` (show needs a UUID). "
    "If your client loads tool schemas on demand, PREDICT the names you need from this "
    "pattern and load them in ONE batch by name — don't fuzzy keyword-search one at a time.\n"
    "- `whoami` exists on every mount (core).\n"
    "- `*_list` tools take `all_projects=True` for the admin/all view where supported; "
    "they return key columns by default — pass `detail=True` for all fields (or use "
    "the matching `_show`), and `limit=N` caps the row count (0=all).\n"
    "- Every result is wrapped in an envelope: `{backend, backend_reason, data}`.\n"
    "- On failure the error message is `Error executing tool <name>: ` followed by a "
    "JSON object `{\"error\": {\"type\", \"message\"}}` — parse it from the first `{`.\n"
    "- service_status covers ONLY Nova/Neutron; for Cinder service health use "
    "volume_service_list (storage)."
)


def env_set(var, allowed):
    raw = os.environ.get(var, "")
    if not raw.strip():
        return set(allowed)
    sel = {x.strip() for x in raw.split(",") if x.strip()}
    return sel & set(allowed)


def instructions(domains, tiers, *, brand="OpenStack", gist=None, conventions=None):
    """Build the initialize `instructions` text for a mount serving `domains`/`tiers`.
    gist/conventions default to core's neutral values; a layer built on top of core
    can override both with richer, multi-backend-aware content via the gist=/
    conventions= params (also threaded through make_mcp/build_http_app below)."""
    gist = DOMAIN_GIST if gist is None else gist
    conventions = CONVENTIONS if conventions is None else conventions
    mapped = [d for d in DOMAINS if d in domains and d in gist]
    if len(mapped) == 1:
        here = f"You are connected to the {brand} MCP — **{mapped[0]}** domain ({gist[mapped[0]]})."
    else:
        here = f"You are connected to the {brand} MCP (combined; all domains)."
    routing = "Domain routing map:\n" + "\n".join(
        f"- {d}: {gist[d]}" for d in DOMAINS if d in gist)
    return f"{scope_line(tiers)}\n\n{conventions}\n\n{here}\n\n{routing}"


def error_json(e: Exception) -> str:
    return json.dumps({"error": {"type": type(e).__name__, "message": str(e)}},
                      ensure_ascii=False)


def wrap_tool_errors(fn, *, error_json=error_json):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*a, **k):
            try:
                return await fn(*a, **k)
            except UrlElicitationRequiredError:
                raise
            except Exception as e:
                raise RuntimeError(error_json(e)) from e
        return aw

    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except UrlElicitationRequiredError:
            raise
        except Exception as e:
            raise RuntimeError(error_json(e)) from e
    return w


def tool_annotations(name, tier):
    read_only = tier == "read"
    return ToolAnnotations(readOnlyHint=read_only,
                           destructiveHint=(not read_only and name.endswith("_delete")))


def make_mcp(reg, name, domains, tiers, *, wrap=wrap_tool_errors, brand="OpenStack",
             gist=None, conventions=None):
    m = FastMCP(name, transport_security=_TS, stateless_http=False,
                instructions=instructions(domains, tiers, brand=brand,
                                          gist=gist, conventions=conventions))
    for t in reg.tools:
        if (t["domain"] == "core" or t["domain"] in domains) and t["tier"] in tiers:
            m.add_tool(wrap(t["fn"]), name=t["name"], description=t["description"],
                       annotations=tool_annotations(t["name"], t["tier"]))
    for p in reg.prompts:
        if p["domain"] == "core" or p["domain"] in domains:
            m.add_prompt(p["prompt"])
    return m


def build_http_app(reg, *, brand="OpenStack", wrap=wrap_tool_errors, extra_routes=None,
                   gist=None, conventions=None):
    """extra_routes: optional list of additional Starlette Route/Mount objects the
    caller wants mounted alongside the per-domain MCP apps (e.g. a management
    layer's plain HTTP endpoints). Core itself never passes any; core stays
    unaware of what (if anything) they do."""
    import contextlib
    from starlette.applications import Starlette
    from starlette.routing import Mount

    active_domains = env_set("MCP_DOMAINS", DOMAINS)
    active_tiers = env_set("MCP_TIERS", TIERS)
    domain_tools = {t["domain"] for t in reg.tools if t["tier"] in active_tiers}
    mounts = {d: make_mcp(reg, f"{brand.lower()}-{d}", {d}, active_tiers, wrap=wrap, brand=brand,
                          gist=gist, conventions=conventions)
              for d in DOMAINS if d in active_domains and d in domain_tools}
    apps = {d: m.streamable_http_app() for d, m in mounts.items()}
    instances = list(mounts.values())
    routes = list(extra_routes or []) + [Mount(f"/{d}", app=a) for d, a in apps.items()]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for inst in instances:
                await stack.enter_async_context(inst.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan), sorted(mounts)

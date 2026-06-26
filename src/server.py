"""Adaptive OpenStack / OpenStackit MCP server.

ONE codebase, TWO editions (chosen at packaging time by file presence):
  - 'openstackit' edition: opit_backend.py + openstackit_client.py present →
    adaptive (OpenStackit-first with OpenStack fallback, plus Key Manager tools).
  - 'openstack' edition: those files absent → runs OpenStack-only automatically.

Per-caller credentials AND target endpoints come from request headers (env
fallback for local stdio):
  OpenStackit : X-OPIT-Base-Url, X-OPIT-User, X-OPIT-Password, X-OPIT-Domain
  OpenStack   : X-OS-Auth-Url, X-OS-App-Cred-Id, X-OS-App-Cred-Secret

Overlap tools accept backend="auto"|"openstack"|"openstackit" (also via the
X-Prefer-Backend header). List tools accept all_projects (default False = current
project on BOTH backends — consistent; True = admin/all-projects view). Every
result carries `backend`.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import typing

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import UrlElicitationRequiredError

import os_backend
import ops_backend
from errors import OpenStackitError
from router import route

try:
    import opit_backend
    HAS_OPIT = True
except ImportError:
    HAS_OPIT = False

OPIT_BASE = os.environ.get("OPENSTACKIT_BASE_URL", "https://192.168.140.14:5529")
OPIT_VERIFY = os.environ.get("OPENSTACKIT_VERIFY_TLS", "false").lower() == "true"
OS_AUTH_URL = os.environ.get("OS_AUTH_URL", "http://192.168.140.14:5000/v3")

# Port is the single source of truth (MCP_PORT, default 8001; --port overrides at
# launch). The Host-header allowlist is DERIVED from the port so changing the port
# alone doesn't break the transport_security check (set MCP_ALLOWED_HOSTS to override
# the host list, or MCP_ALLOWED_HOST_NAMES to add hostnames beyond the defaults).
MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
_HOST_NAMES = [h.strip() for h in os.environ.get(
    "MCP_ALLOWED_HOST_NAMES", "192.168.140.14,localhost,127.0.0.1").split(",") if h.strip()]
_DEFAULT_HOSTS = ",".join(f"{h}:{MCP_PORT}" for h in _HOST_NAMES)
_HOSTS = os.environ.get("MCP_ALLOWED_HOSTS", _DEFAULT_HOSTS)

_TS = TransportSecuritySettings(
    allowed_hosts=[h.strip() for h in _HOSTS.split(",") if h.strip()], allowed_origins=["*"])

osp = os_backend.OpenStackProvider(OS_AUTH_URL)
opitp = opit_backend.OpenStackitProvider(OPIT_BASE, OPIT_VERIFY) if HAS_OPIT else None


# --------------------------------------------------------------------------- #
# Context / credentials / endpoints
# --------------------------------------------------------------------------- #
def _headers(ctx):
    try:
        return ctx.request_context.request.headers
    except Exception:
        return {}


def _os_auth_url(ctx):
    return _headers(ctx).get("x-os-auth-url") or OS_AUTH_URL


def _opit_base(ctx):
    return _headers(ctx).get("x-opit-base-url") or OPIT_BASE


def _os_creds(ctx):
    h = _headers(ctx)
    cid = h.get("x-os-app-cred-id") or os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
    sec = h.get("x-os-app-cred-secret") or os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    return (cid, sec) if cid and sec else None


def _os_conn(ctx):
    creds = _os_creds(ctx)
    return osp.conn(*creds, auth_url=_os_auth_url(ctx)) if creds else None


def _opit_creds(ctx):
    h = _headers(ctx)
    user = h.get("x-opit-user") or os.environ.get("OPIT_USER")
    pw = h.get("x-opit-password") or os.environ.get("OPIT_PASSWORD")
    domain = h.get("x-opit-domain") or os.environ.get("OPIT_DOMAIN") or "default"
    return (user, pw, domain) if user and pw else None


def _opit_session(ctx):
    if not HAS_OPIT:
        return None
    creds = _opit_creds(ctx)
    return opitp.session(*creds, base_url=_opit_base(ctx)) if creds else None


def _opit_usable(ctx) -> bool:
    # Cheap routing gate (used per-call): credentials present + server reachable.
    # NOT a real auth check — the session auto-re-logins on expiry, so this only
    # decides whether to attempt OpenStackit at all. whoami uses _opit_alive for truth.
    return HAS_OPIT and opitp.available(_opit_base(ctx)) and _opit_creds(ctx) is not None


def _agent_mode(ctx, action: str) -> str:
    """action: 'create' | 'delete'. Returns this session's mode for that action —
    'form' (default) or 'native'."""
    sess = _opit_session(ctx)
    return getattr(sess, f"agent_mode_{action}", "form") if sess else "form"


async def agent_mode(ctx: Context, create: str = "", delete: str = "") -> dict:
    """Get or set the agent interaction mode for create and delete SEPARATELY, for
    THIS session. Each is "form" (default — a human form/confirm via elicitation) or
    "native" (the agent acts directly: create takes body=, delete deletes immediately,
    NO elicitation). An empty arg leaves that flag unchanged; both empty → just report.
    Asymmetric gate: switching DELETE form→native needs a human confirmation (delete is
    irreversible); CREATE switches freely both ways, and delete native→form is immediate.
    Volatile: resets to "form" on reconnect/re-login."""
    sess = _opit_session(ctx)
    if sess is None:
        raise RuntimeError("requires OpenStackit credentials")
    for nm, val in (("create", create), ("delete", delete)):
        if val not in ("", "form", "native"):
            raise RuntimeError(f"{nm} must be 'form' or 'native'")
    out = {"create": getattr(sess, "agent_mode_create", "form"),
           "delete": getattr(sess, "agent_mode_delete", "form")}
    extra = {}
    # create: no gate, either direction.
    if create:
        sess.agent_mode_create = create
        out["create"] = create
    # delete: form→native needs a human confirm; native→form (and no-op) is immediate.
    if delete:
        cur = getattr(sess, "agent_mode_delete", "form")
        if delete == "native" and cur != "native":
            from pydantic import create_model, Field
            Confirm = create_model("ConfirmNativeDelete", choice=(str, Field(
                ..., description=("Switch DELETE to NATIVE mode? The agent will then delete "
                                  "resources DIRECTLY with no human confirmation — irreversible. "
                                  "Switch back with agent_mode delete=form."),
                json_schema_extra={"enum": ["native", "cancel"],
                                   "enumNames": ["delete native 확정", "취소"]})))
            res = await ctx.elicit(message="agent_mode delete → native 확인", schema=Confirm)
            if (getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None
                    or res.data.model_dump().get("choice") != "native"):
                extra["delete_cancelled"] = True
            else:
                sess.agent_mode_delete = "native"
                out["delete"] = "native"
        else:
            sess.agent_mode_delete = delete
            out["delete"] = delete
    return {"agent_mode": out, **extra}


def _opit_alive(ctx) -> dict:
    """Real authentication check (for whoami): make ONE light authed call. The
    session re-logins on expiry, so this reflects whether OpenStackit is actually
    usable right now — not merely that credentials exist."""
    if not HAS_OPIT:
        return {"usable": False, "reason": "openstack edition (no OpenStackit)"}
    if _opit_creds(ctx) is None:
        return {"usable": False, "reason": "no OpenStackit credentials"}
    if not opitp.available(_opit_base(ctx)):
        return {"usable": False, "reason": "OpenStackit unreachable"}
    try:
        sess = _opit_session(ctx)  # may log in here (can raise on bad credentials)
        if sess is None:
            return {"usable": False, "reason": "no session"}
        sess.get("/api/identity/projects/list")
        return {"usable": True}
    except OpenStackitError as e:
        return {"usable": False, "reason": f"auth probe failed (HTTP {e.http_status})"}
    except Exception as e:  # pragma: no cover - defensive
        return {"usable": False, "reason": f"auth probe error: {type(e).__name__}"}


def _opit_logout(ctx) -> dict:
    """Revoke + evict the caller's cached OpenStackit session (header credentials)."""
    if not HAS_OPIT:
        return {"logged_out": False, "note": "openstack edition (no OpenStackit)"}
    creds = _opit_creds(ctx)
    if creds is None:
        return {"logged_out": False, "note": "no OpenStackit credentials"}
    revoked = opitp.logout(*creds, base_url=_opit_base(ctx))
    return {"backend": "openstackit", "logged_out": True, "revoked": revoked,
            "note": "next call re-logins with the current header credentials"}


def _prefer(ctx, backend_arg: str = "") -> str:
    if backend_arg and backend_arg != "auto":
        return backend_arg
    return (_headers(ctx).get("x-prefer-backend")
            or os.environ.get("OPIT_PREFER_BACKEND") or "auto")


# cpuinfo = openstacksdk Hypervisor 레거시 별칭(정식 cpu_info와 완전 동일한 거대 객체) → 드롭.
_DROP_KEYS = {"location", "links", "_csrf", "cpuinfo"}


def _full(item):
    """Return ALL meaningful fields of an item (openstacksdk object → to_dict, or
    OpenStackit JSON dict as-is), dropping empty/null values and a little noise.
    Maximizes the data shown rather than picking a fixed handful of fields."""
    try:
        d = item if isinstance(item, dict) else item.to_dict()
    except Exception:
        d = dict(item) if isinstance(item, dict) else {"value": str(item)}
    out = {}
    for k, v in d.items():
        if k in _DROP_KEYS or v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def _summary(item, fields):
    """list용 축약 뷰: _full 결과에서 `fields`에 있는 키만 남긴다.
    필드명이 이 백엔드의 응답 모양과 하나도 안 맞으면(투영 결과가 빔) 전체를
    그대로 반환한다 — 응답이 통째로 비는 일은 없게 하는 비파괴 안전장치."""
    full = _full(item)
    if not fields:
        return full
    picked = {k: full[k] for k in fields if k in full}
    return picked or full


# OpenStack project-scoping idioms differ by service. These helpers return a
# lister fn(conn, all_projects) so the table can stay declarative.
def _nova(method):   # nova/cinder: native all_projects flag
    return lambda c, a: getattr(c.compute, method)(all_projects=a)


def _cinder(method):
    return lambda c, a: getattr(c.block_storage, method)(all_projects=a)


def _neutron(method):  # neutron: scope by project_id filter, or unfiltered (admin) for all
    return lambda c, a: (getattr(c.network, method)() if a
                         else getattr(c.network, method)(project_id=c.current_project_id))


def _octavia(method):
    return lambda c, a: (getattr(c.load_balancer, method)() if a
                         else getattr(c.load_balancer, method)(project_id=c.current_project_id))


def _plain(getter):   # non-scoped / global: ignore all_projects
    return lambda c, a: getter(c)


# --------------------------------------------------------------------------- #
# Domain/tier registry (multimount). Tools are collected here with (domain, tier)
# tags instead of bound to a single FastMCP; make_mcp() builds per-domain
# instances from this registry. See docs/superpowers/specs/2026-06-19-multimount-*.
# --------------------------------------------------------------------------- #
DOMAINS = ["compute", "network", "lbaas", "storage", "image", "identity",
           "keymanager", "observability", "billing", "orchestration"]
TIERS = ["read", "write", "maintain"]

_REGISTRY: list[dict] = []   # [{fn, name, description, domain, tier}]
_PROMPTS: list[dict] = []    # [{prompt, domain}]


def add(fn, *, name, description, domain, tier="read"):
    """Register a tool with (domain, tier) tags. 'core' domain = present in every mount."""
    _REGISTRY.append(dict(fn=fn, name=name, description=description,
                          domain=domain, tier=tier))


def add_prompt(prompt, *, domain):
    _PROMPTS.append(dict(prompt=prompt, domain=domain))


# --------------------------------------------------------------------------- #
# Special tools (hand-written). Defined as plain functions, registered via add().
# --------------------------------------------------------------------------- #
def _current_context(ctx):
    """Best-effort: the caller's current project {id,name} and their roles in it.
    OpenStackit-first (roleCode is the governance role the user asked about), with
    an OpenStack token fallback. Returns None if neither backend can resolve it.
    Every sub-lookup is individually guarded so a partial failure still yields what
    it can — whoami's core response must never break on this."""
    if _opit_usable(ctx):
        try:
            sess = _opit_session(ctx)
            me = sess.get("/api/myaccount") or {}
            # current scope = project switched to via switch_project, else account
            # default. myaccount only reports the DEFAULT, so a switch would
            # otherwise be invisible here.
            uid = me.get("userId")
            pid = getattr(sess, "current_project", None) or me.get("default_project_id")
            if not uid or not pid:
                return None
            name = None
            try:
                name = (sess.get(f"/api/identity/projects/{pid}/detail") or {}).get("name")
            except Exception:
                pass
            roles = []
            try:
                rs = sess.get(f"/api/identity/users/{uid}/roles") or []
                roles = sorted({r.get("roleCode") for r in rs
                                if r.get("projectId") == pid and r.get("roleCode")})
            except Exception:
                pass
            return {"current_project": {"id": pid, "name": name},
                    "roles": roles, "roles_source": "openstackit"}
        except Exception:
            pass
    conn = _os_conn(ctx)
    if conn:
        try:
            acc = conn.session.auth.get_access(conn.session)
            return {"current_project": {"id": conn.current_project_id,
                                        "name": getattr(acc, "project_name", None)},
                    "roles": list(getattr(acc, "role_names", []) or []),
                    "roles_source": "openstack"}
        except Exception:
            pass
    return None


def whoami(ctx: Context) -> dict:
    """Show which backends are configured/reachable for the caller. The OpenStackit
    `usable` flag is a REAL auth probe (one light authed call), not just credential
    presence — so an expired/unauthenticated session reports usable=false with a reason."""
    alive = _opit_alive(ctx)
    opit = {"base_url": _opit_base(ctx),
            "installed": HAS_OPIT and opitp.available(_opit_base(ctx)),
            "credentials": _opit_creds(ctx) is not None, "usable": alive["usable"]}
    if not alive["usable"] and "reason" in alive:
        opit["reason"] = alive["reason"]
    result = {
        "edition": "openstackit" if HAS_OPIT else "openstack",
        "openstackit": opit,
        "openstack": {"auth_url": _os_auth_url(ctx), "credentials": _os_creds(ctx) is not None},
        "agent_mode": {"create": _agent_mode(ctx, "create"),
                       "delete": _agent_mode(ctx, "delete")},
    }
    # best-effort context (current project + roles); whoami core is built above.
    ctxinfo = _current_context(ctx)
    if ctxinfo:
        result.update(ctxinfo)
    else:
        result["context_note"] = "could not resolve current project/roles (no usable backend)"
    return result


def server_stop(ctx: Context, server_id: str) -> dict:
    """Stop (power off) a compute instance. OpenStack backend."""
    conn = _os_conn(ctx)
    return route(policy="openstack_only",
                 openstack=(lambda: os_backend.server_stop(conn, server_id)) if conn else None)


def server_start(ctx: Context, server_id: str) -> dict:
    """Start (power on) a compute instance. OpenStack backend."""
    conn = _os_conn(ctx)
    return route(policy="openstack_only",
                 openstack=(lambda: os_backend.server_start(conn, server_id)) if conn else None)


def quota_show(ctx: Context, project_id: str = "", backend: str = "auto") -> dict:
    """Project quota + usage across compute / network / block storage / share.
    Defaults to YOUR current project; pass project_id for another project.
    OpenStackit-first with OpenStack fallback. Note: the 'usage' (used-vs-max
    AbsoluteLimit) is only available for your CURRENT project on OpenStackit; for
    a different project_id only limits are returned (use backend='openstack' for
    that project's in_use via the SDK)."""
    conn, sess = _os_conn(ctx), _opit_session(ctx)

    def _opit():
        # current scope = switch_project target, else account default (myaccount
        # only reports the default).
        cur = (getattr(sess, "current_project", None)
               or (sess.get("/api/myaccount") or {}).get("default_project_id"))
        pid = project_id or cur
        out = {"project_id": pid, "limits": {}}
        try:
            u = sess.get(f"/api/limits/project/{pid}/quota") or {}
            out["limits"] = {"compute": u.get("computeQuotaSet"),
                             "network": u.get("networkQuotaSet"),
                             "block_storage": u.get("blockStorageQuotaSet"),
                             "share": u.get("shareFileSystemQuotaSet")}
        except Exception as e:
            out["limits"] = {"error": str(e)}
        if not project_id or project_id == cur:
            try:
                out["usage"] = sess.get("/api/limits/project/compute/limit")
            except Exception as e:
                out["usage"] = {"error": str(e)}
        else:
            out["usage_note"] = ("AbsoluteLimit (used vs max) is current-project only; "
                                 "limits shown. Use backend='openstack' for this project's in_use.")
        return out

    return route(policy="auto", prefer=_prefer(ctx, backend), openstackit_available=_opit_usable(ctx),
                 openstackit=_opit if sess else None,
                 openstack=(lambda: os_backend.quota_show(conn, project_id or conn.current_project_id))
                 if conn else None)


def capacity_stats(ctx: Context) -> dict:
    """Aggregate compute capacity vs usage (Placement). OpenStack backend."""
    conn = _os_conn(ctx)
    return route(policy="openstack_only",
                 openstack=(lambda: os_backend.capacity_stats(conn)) if conn else None)


def service_status(ctx: Context) -> dict:
    """Node/service health: ONLY Nova compute services + Neutron network agents
    (up/down). Does NOT cover storage/image — for Cinder service health (e.g.
    cinder-backup/-volume/-scheduler down) use `volume_service_list` in the storage
    domain. OpenStack backend."""
    conn = _os_conn(ctx)
    return route(policy="openstack_only",
                 openstack=(lambda: os_backend.service_status(conn)) if conn else None)


def log_targets(ctx: Context, node: str = "") -> dict:
    """List log targets. node='' → known node list + this (base) node's targets;
    node='<name>' → that node's targets (proxied). Call with node='' first to see
    which nodes are registered (LOG_NODES). OpenStack(it) host log files."""
    return ops_backend.targets_for(node)


def log_tail(ctx: Context, target: str, lines: int = 300, grep: str = "",
             node: str = "", since: str = "", until: str = "", last: str = "") -> dict:
    """한 target 로그를 시간창 안에서 구조화·시간순으로 반환. since/until(절대 '2026-06-25 14:30'
    또는 '14:30') 또는 last('30m'/'2h'/'1d'); 모두 비면 최근 30분. grep=정규식 필터, node=다른 노드.
    결과의 cursor를 다음 호출 since로 주면 새 줄만(폴링).
    target 예: 'kolla:nova', 'openstackit:servlet'."""
    return ops_backend.tail_for(target, lines=lines, grep=grep, node=node,
                                since=since, until=until, last=last)


def log_trace(ctx: Context, id: str, since: str = "", until: str = "", last: str = "",
              nodes: str = "", targets: str = "") -> dict:
    """한 요청/트레이스 ID(OpenStack 'req-...' 또는 OpenStackit trace uuid)가 거쳐간 로그를
    여러 서비스·노드에서 모아 시간순으로 돌려준다. nodes=''(로컬)/'all'(LOG_NODES 전체)/'c1,c2'.
    targets로 서비스 한정. 시간창은 since/until 또는 last(기본 30m). cursor로 폴링.
    주의: OpenStack(req-)과 OpenStackit(trace=)는 ID 체계가 달라 같은 세계 안에서만 이어진다."""
    return ops_backend.trace_for(id, since=since, until=until, last=last,
                                 nodes=nodes, targets_csv=targets)


add(whoami, name="whoami", domain="core", tier="read",
    description=whoami.__doc__)
add(agent_mode, name="agent_mode", domain="core", tier="read",
    description=agent_mode.__doc__)
add(server_stop, name="server_stop", domain="compute", tier="write",
    description=server_stop.__doc__)
add(server_start, name="server_start", domain="compute", tier="write",
    description=server_start.__doc__)
add(quota_show, name="quota_show", domain="compute", tier="read",
    description=quota_show.__doc__)
add(capacity_stats, name="capacity_stats", domain="compute", tier="read",
    description=capacity_stats.__doc__)
add(service_status, name="service_status", domain="observability", tier="read",
    description=service_status.__doc__)
add(log_targets, name="log_targets", domain="observability", tier="read",
    description=log_targets.__doc__)
add(log_tail, name="log_tail", domain="observability", tier="read",
    description=log_tail.__doc__)
add(log_trace, name="log_trace", domain="observability", tier="read",
    description=log_trace.__doc__)


# --------------------------------------------------------------------------- #
# Overlap resources — list/show generated from a declarative table.
#   os_list: fn(conn, all_projects)         opit_list: project-scoped path
#   opit_list_all: all-projects path or None (None → all_projects served by OpenStack)
# Default all_projects=False → BOTH backends scope to the current project.
# --------------------------------------------------------------------------- #
RESOURCES = [
    dict(name="server", fields=["id", "name", "status"],
         os_list=_nova("servers"), opit_list="/api/compute/servers", opit_list_all="/api/compute/servers/list/all",
         os_show=lambda c, i: c.compute.get_server(c.compute.find_server(i, ignore_missing=False).id),
         opit_show="/api/compute/servers/{id}",
         os_update=lambda c, i, b: c.compute.update_server(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.compute.delete_server(i, ignore_missing=False)),
    dict(name="flavor", fields=["id", "name", "vcpus", "ram", "disk"],
         os_list=_plain(lambda c: c.compute.flavors()), opit_list="/api/compute/flavors",
         os_show=lambda c, i: c.compute.get_flavor(i), opit_show="/api/compute/flavors/{id}",
         os_delete=lambda c, i: c.compute.delete_flavor(i, ignore_missing=False)),
    dict(name="keypair", fields=["name", "fingerprint", "type"],
         os_list=_plain(lambda c: c.compute.keypairs()), opit_list="/api/compute/os-keypairs",
         opit_delete="/api/compute/os-keypairs/{id}",
         os_delete=lambda c, i: c.compute.delete_keypair(i, ignore_missing=False)),
    dict(name="hypervisor", fields=["id", "name", "status", "state", "hypervisor_type", "hypervisor_hostname"],
         os_list=_plain(lambda c: c.compute.hypervisors(details=True)), opit_list="/api/compute/os-hypervisors/list"),
    dict(name="availability_zone", fields=["name", "state", "zoneName", "zoneState"],
         os_list=_plain(lambda c: c.compute.availability_zones()), opit_list="/api/compute/servers/availability-zone"),
    dict(name="network", fields=["id", "name", "status", "is_router_external"],
         os_list=_neutron("networks"), opit_list="/api/networking/networks", opit_list_all="/api/networking/networks/list/all",
         os_show=lambda c, i: c.network.get_network(i), opit_show="/api/networking/networks/{id}",
         os_delete=lambda c, i: c.network.delete_network(i, ignore_missing=False),
         os_update=lambda c, i, b: c.network.update_network(i, **b),
         update_fields=["name", "description"]),
    dict(name="subnet", fields=["id", "name", "cidr", "ip_version", "network_id"],
         os_list=_neutron("subnets"), opit_list="/api/networking/subnets", opit_list_all="/api/networking/subnets/list/all",
         os_show=lambda c, i: c.network.get_subnet(i), opit_show="/api/networking/subnets/{id}",
         os_update=lambda c, i, b: c.network.update_subnet(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_subnet(i, ignore_missing=False)),
    dict(name="router", fields=["id", "name", "status"],
         os_list=_neutron("routers"), opit_list="/api/networking/routers", opit_list_all="/api/networking/routers/list/all",
         os_show=lambda c, i: c.network.get_router(i), opit_show="/api/networking/routers/{id}",
         os_update=lambda c, i, b: c.network.update_router(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_router(i, ignore_missing=False)),
    dict(name="port", fields=["id", "name", "status", "mac_address", "network_id"],
         os_list=_neutron("ports"), opit_list="/api/networking/ports", opit_list_all="/api/networking/ports/list/all",
         os_show=lambda c, i: c.network.get_port(i), opit_show="/api/networking/ports/{id}",
         os_update=lambda c, i, b: c.network.update_port(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_port(i, ignore_missing=False)),
    dict(name="security_group", fields=["id", "name", "description"],
         os_list=_neutron("security_groups"), opit_list="/api/networking/security-groups",
         opit_list_all="/api/networking/security-groups/list/all",
         os_show=lambda c, i: c.network.get_security_group(i), opit_show="/api/networking/security-groups/{id}",
         os_update=lambda c, i, b: c.network.update_security_group(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_security_group(i, ignore_missing=False)),
    dict(name="security_group_rule",
         fields=["id", "direction", "protocol", "port_range_min", "port_range_max", "ethertype"],
         os_list=_neutron("security_group_rules"), opit_list="/api/networking/security-group-rules",
         opit_list_all="/api/networking/security-group-rules/list/all",
         opit_delete="/api/networking/security-group-rules/{id}",
         os_delete=lambda c, i: c.network.delete_security_group_rule(i, ignore_missing=False)),
    dict(name="floating_ip", fields=["id", "floating_ip_address", "fixed_ip_address", "status", "port_id"],
         os_list=_neutron("ips"), opit_list="/api/networking/floatingips", opit_list_all="/api/networking/floatingips/list/all",
         os_show=lambda c, i: c.network.get_ip(i), opit_show="/api/networking/floatingips/{id}",
         os_update=lambda c, i, b: c.network.update_ip(i, **b),
         update_fields=["description", "port_id"],
         os_delete=lambda c, i: c.network.delete_ip(i, ignore_missing=False)),
    dict(name="volume", fields=["id", "name", "status", "size"],
         os_list=_cinder("volumes"), opit_list="/api/storage/block/volumes/list", opit_list_all="/api/storage/block/volumes/list/all",
         os_show=lambda c, i: c.block_storage.get_volume(i), opit_show="/api/storage/block/volumes/{id}",
         os_update=lambda c, i, b: c.block_storage.update_volume(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.block_storage.delete_volume(i, ignore_missing=False)),
    dict(name="volume_snapshot", fields=["id", "name", "status", "size", "volume_id"],
         os_list=_cinder("snapshots"), opit_list="/api/storage/block/snapshots/list",
         os_show=lambda c, i: c.block_storage.get_snapshot(i), opit_show="/api/storage/block/snapshots/{id}",
         os_delete=lambda c, i: c.block_storage.delete_snapshot(i, ignore_missing=False)),
    dict(name="volume_type", fields=["id", "name", "is_public"],
         os_list=_plain(lambda c: c.block_storage.types()), opit_list="/api/storage/block/types"),
    dict(name="volume_backup", fields=["id", "name", "status", "size"],
         os_list=_cinder("backups"), opit_list="/api/storage/block/backups/list",
         os_show=lambda c, i: c.block_storage.get_backup(i), opit_show="/api/storage/block/backups/{id}",
         os_delete=lambda c, i: c.block_storage.delete_backup(i, ignore_missing=False)),
    dict(name="image", fields=["id", "name", "status", "disk_format", "visibility", "size"],
         os_list=lambda c, a: (c.image.images() if a else c.image.images(owner=c.current_project_id)),
         opit_list="/api/images/v2", opit_list_all="/api/images/v2/list/all",
         os_show=lambda c, i: c.image.get_image(i), opit_show="/api/images/v2/{id}",
         os_delete=lambda c, i: c.image.delete_image(i, ignore_missing=False)),
    dict(name="project", fields=["id", "name", "description"],
         os_list=_plain(lambda c: c.identity.projects()), opit_list="/api/identity/projects/list",
         os_show=lambda c, i: c.identity.get_project(i), opit_show="/api/identity/projects/{id}/detail",
         opit_delete="/api/identity/projects/{id}/delete",
         os_delete=lambda c, i: c.identity.delete_project(i, ignore_missing=False)),
    dict(name="domain", fields=["id", "name", "description"],
         os_list=_plain(lambda c: c.identity.domains()), opit_list="/api/identity/domains",
         os_show=lambda c, i: c.identity.get_domain(i), opit_show="/api/identity/domains/{id}",
         os_delete=lambda c, i: c.identity.delete_domain(i, ignore_missing=False)),
    dict(name="load_balancer", fields=["id", "name", "provisioning_status", "operating_status", "vip_address"],
         os_list=_octavia("load_balancers"), opit_list="/api/lbaas/loadbalancers", opit_list_all="/api/lbaas/loadbalancers/list/all",
         os_show=lambda c, i: c.load_balancer.get_load_balancer(i), opit_show="/api/lbaas/loadbalancers/{id}",
         os_update=lambda c, i, b: c.load_balancer.update_load_balancer(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_load_balancer(i, ignore_missing=False)),
    dict(name="listener", fields=["id", "name", "protocol", "protocol_port"],
         os_list=_octavia("listeners"), opit_list="/api/lbaas/listeners", opit_list_all="/api/lbaas/listeners/list/all",
         os_show=lambda c, i: c.load_balancer.get_listener(i), opit_show="/api/lbaas/listeners/{id}",
         os_update=lambda c, i, b: c.load_balancer.update_listener(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_listener(i, ignore_missing=False)),
    dict(name="pool", fields=["id", "name", "protocol", "lb_algorithm"],
         os_list=_octavia("pools"), opit_list="/api/lbaas/pools", opit_list_all="/api/lbaas/pools/list/all",
         os_show=lambda c, i: c.load_balancer.get_pool(i), opit_show="/api/lbaas/pools/{id}",
         os_update=lambda c, i, b: c.load_balancer.update_pool(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_pool(i, ignore_missing=False)),
    dict(name="role", fields=["id", "name", "code", "alias", "domainId", "enabled"],
         os_list=_plain(lambda c: c.identity.roles()), opit_list="/api/identity/roles",
         opit_delete="/api/identity/roles/{id}/delete",
         os_delete=lambda c, i: c.identity.delete_role(i, ignore_missing=False)),
    # --- TIER 1: Identity ---
    # user: response is portal RegisterDto (not raw Keystone user). Admin-scoped.
    # list path confirmed: /api/identity/users/list (also /api/identity/users same result)
    # show path confirmed: /api/identity/users/{userId}/detail (userId is username string, not UUID)
    dict(name="user", fields=["userId", "domainId", "state", "alias", "email", "organization",
                              "id", "name", "enabled", "default_project_id"],  # both backends: OpenStackit DTO + SDK keys
         os_list=_plain(lambda c: c.identity.users()), opit_list="/api/identity/users/list",
         opit_list_all="/api/identity/users/listAll",
         os_show=lambda c, i: c.identity.get_user(i),
         opit_show="/api/identity/users/{id}/detail",
         opit_update="/api/identity/users/{id}",
         os_update=lambda c, i, b: c.identity.update_user(i, **b),
         update_fields=["name", "email"],
         opit_delete="/api/identity/users/{id}",
         os_delete=lambda c, i: c.identity.delete_user(i, ignore_missing=False)),
    # role_assignment: admin-scoped, no show endpoint
    dict(name="role_assignment",
         fields=["userId", "roleCode", "alias", "projectId", "domainId"],
         os_list=_plain(lambda c: c.identity.role_assignments()),
         opit_list="/api/identity/role_assignments"),
    # application_credential: scoped to current user on SDK side
    dict(name="application_credential",
         fields=["id", "name", "description", "expires_at"],
         os_list=_plain(lambda c: c.identity.application_credentials(c.current_user_id)),
         opit_list="/api/identity/application_credentials/list"),
    # region: globally readable
    dict(name="region", fields=["id", "description", "parent_region_id"],
         os_list=_plain(lambda c: c.identity.regions()), opit_list="/api/identity/regions/list",
         os_show=lambda c, i: c.identity.get_region(i)),
    # service: admin-scoped catalog service entries
    dict(name="service", fields=["id", "name", "type", "description", "enabled"],
         os_list=_plain(lambda c: c.identity.services()), opit_list="/api/identity/services",
         os_show=lambda c, i: c.identity.get_service(i)),
    # endpoint: admin-scoped catalog endpoints
    dict(name="endpoint",
         fields=["id", "service_id", "interface", "region", "url", "enabled"],
         os_list=_plain(lambda c: c.identity.endpoints()), opit_list="/api/identity/endpoints/list",
         os_show=lambda c, i: c.identity.get_endpoint(i)),
    # --- TIER 2: Networking ---
    # agent: admin/global neutron agents
    dict(name="agent", fields=["id", "agent_type", "binary", "host", "alive", "admin_state_up"],
         os_list=_plain(lambda c: c.network.agents()), opit_list="/api/networking/agents",
         os_show=lambda c, i: c.network.get_agent(i), opit_show="/api/networking/agents/{id}"),
    # rbac_policy: project-scoped RBAC policies
    dict(name="rbac_policy", fields=["id", "object_type", "object_id", "action", "target_tenant", "project_id"],
         os_list=_neutron("rbac_policies"), opit_list="/api/networking/rbac-policies",
         os_show=lambda c, i: c.network.get_rbac_policy(i), opit_show="/api/networking/rbac-policies/{id}"),
    # network_ip_availability: admin. identifier is network_id (not a plain UUID field named id)
    dict(name="network_ip_availability",
         fields=["network_id", "network_name", "total_ips", "used_ips", "subnet_ip_availability"],
         os_list=_plain(lambda c: c.network.network_ip_availabilities()),
         opit_list="/api/networking/network-ip-availabilities",
         os_show=lambda c, i: c.network.get_network_ip_availability(i),
         opit_show="/api/networking/network-ip-availabilities/{id}"),
    # --- TIER 2: LBaaS ---
    # health_monitor: project-scoped
    dict(name="health_monitor", fields=["id", "name", "type", "delay", "timeout", "max_retries", "operating_status"],
         os_list=_octavia("health_monitors"), opit_list="/api/lbaas/healthmonitors",
         os_show=lambda c, i: c.load_balancer.get_health_monitor(i), opit_show="/api/lbaas/healthmonitors/{id}",
         os_update=lambda c, i, b: c.load_balancer.update_health_monitor(i, **b),
         update_fields=["name"],
         os_delete=lambda c, i: c.load_balancer.delete_health_monitor(i, ignore_missing=False)),
    # l7_policy: project-scoped
    dict(name="l7_policy", fields=["id", "name", "action", "listener_id", "position", "provisioning_status"],
         os_list=_octavia("l7_policies"), opit_list="/api/lbaas/l7policies",
         os_show=lambda c, i: c.load_balancer.get_l7_policy(i), opit_show="/api/lbaas/l7policies/{id}",
         os_delete=lambda c, i: c.load_balancer.delete_l7_policy(i, ignore_missing=False)),
    # lb_flavor: global (not project-scoped) octavia flavor — distinct from compute flavor
    dict(name="lb_flavor", fields=["id", "name", "description", "flavor_profile_id", "enabled"],
         os_list=_plain(lambda c: c.load_balancer.flavors()), opit_list="/api/lbaas/flavors",
         os_show=lambda c, i: c.load_balancer.get_flavor(i), opit_show="/api/lbaas/flavors/{id}"),
    # --- TIER 2: Compute ---
    # aggregate: admin-global host aggregates
    dict(name="aggregate", fields=["id", "name", "availability_zone", "hosts", "metadata"],
         os_list=_plain(lambda c: c.compute.aggregates()), opit_list="/api/compute/os-aggregates/list",
         os_show=lambda c, i: c.compute.get_aggregate(i), opit_show="/api/compute/os-aggregates/{id}/detail",
         opit_update="/api/compute/os-aggregates/{id}",
         os_update=lambda c, i, b: c.compute.update_aggregate(i, **b),
         update_fields=["name"],
         opit_delete="/api/compute/os-aggregates/{id}",
         os_delete=lambda c, i: c.compute.delete_aggregate(i, ignore_missing=False)),
    # server_group: admin-global affinity/anti-affinity groups
    dict(name="server_group", fields=["id", "name", "policy", "policies", "members"],
         os_list=_plain(lambda c: c.compute.server_groups()), opit_list="/api/compute/os-server-group",
         os_show=lambda c, i: c.compute.get_server_group(i), opit_show="/api/compute/os-server-group/{id}",
         os_delete=lambda c, i: c.compute.delete_server_group(i, ignore_missing=False)),
    # --- TIER 2: Block Storage ---
    # volume_group: project-scoped volume groups
    dict(name="volume_group", fields=["id", "name", "status", "group_type", "availability_zone"],
         os_list=_cinder("groups"), opit_list="/api/storage/block/groups/list",
         opit_list_all="/api/storage/block/groups/list/all",
         os_show=lambda c, i: c.block_storage.get_group(i), opit_show="/api/storage/block/groups/{id}/detail"),
    # volume_group_type: global group types
    dict(name="volume_group_type", fields=["id", "name", "description", "is_public", "group_specs"],
         os_list=_plain(lambda c: c.block_storage.group_types()), opit_list="/api/storage/block/group_types/list",
         os_show=lambda c, i: c.block_storage.get_group_type(i),
         opit_show="/api/storage/block/group_types/{id}/detail"),
    # volume_group_snapshot: project-scoped group snapshots
    dict(name="volume_group_snapshot", fields=["id", "name", "status", "group_id", "group_type_id"],
         os_list=_cinder("group_snapshots"), opit_list="/api/storage/block/group_snapshots/list",
         opit_list_all="/api/storage/block/group_snapshots/list/all",
         os_show=lambda c, i: c.block_storage.get_group_snapshot(i),
         opit_show="/api/storage/block/group_snapshots/{id}/detail"),
    # volume_service: admin-scoped cinder services. no show endpoint.
    dict(name="volume_service", fields=["binary", "host", "state", "status", "zone", "updated_at"],
         os_list=_plain(lambda c: c.block_storage.services()), opit_list="/api/storage/block/os-services"),
    # --- TIER 2: Image ---
    # metadef_namespace: glance metadata definition namespaces.
    # opit returns dict with 'namespaces' key; use opit_list_key to unwrap.
    # identifier is namespace name (string), not UUID.
    dict(name="metadef_namespace",
         fields=["namespace", "display_name", "description", "visibility", "protected", "owner"],
         os_list=_plain(lambda c: c.image.metadef_namespaces()),
         opit_list="/api/images/v2/metadefs/namespaces", opit_list_key="namespaces",
         os_show=lambda c, i: c.image.get_metadef_namespace(i),
         opit_show="/api/images/v2/metadefs/namespaces/{id}"),
    # --- READ TIER: orchestration (OpenStackit-only; Heat stacks + Magnum/COE) --- #
    # policy="never" + no os_*: OpenStackit value-add, no raw-OpenStack equivalent here.
    # All confirmed to return JSON lists (empty on the dev farm — no stacks/clusters yet).
    dict(name="stack", domain="orchestration", tier="read", policy="never",
         fields=["id", "stack_name", "stack_status"],
         opit_list="/api/orchestration/stacks", opit_list_all="/api/orchestration/stacks/all",
         opit_show="/api/orchestration/stacks/{id}"),
    dict(name="resource_type", domain="orchestration", tier="read", policy="never",
         fields=["resource_type"],
         opit_list="/api/orchestration/resource_types",
         opit_show="/api/orchestration/resource_types/{id}"),
    dict(name="template_version", domain="orchestration", tier="read", policy="never",
         fields=["version", "type"],
         opit_list="/api/orchestration/template_versions",
         opit_show="/api/orchestration/template_versions/{id}"),
    dict(name="cluster", domain="orchestration", tier="read", policy="never",
         fields=["uuid", "name", "status"],
         opit_list="/api/containerinfra/clusters", opit_list_all="/api/containerinfra/clusters/all",
         opit_show="/api/containerinfra/clusters/{id}"),
    dict(name="cluster_template", domain="orchestration", tier="read", policy="never",
         fields=["uuid", "name"],
         opit_list="/api/containerinfra/clustertemplates", opit_list_all="/api/containerinfra/clustertemplates/all",
         opit_show="/api/containerinfra/clustertemplates/{id}"),
    dict(name="coe_quota", domain="orchestration", tier="read", policy="never",
         fields=["id", "project_id", "resource", "hard_limit"],
         opit_list="/api/containerinfra/quotas",
         opit_show="/api/containerinfra/quotas/{id}"),

    # ===================== READ TIER: bulk OpenStackit-only reads ===================== #
    # All OpenStackit value-add (policy="never"); paths + JSON-list shape live-probed.
    # --- billing (new domain) ---
    dict(name="invoice", domain="billing", tier="read", policy="never",
         opit_list="/api/invoice", opit_show="/api/invoice/{id}"),
    dict(name="price", domain="billing", tier="read", policy="never",
         opit_list="/api/price", opit_show="/api/price/{id}"),
    dict(name="promotion", domain="billing", tier="read", policy="never",
         opit_list="/api/promotion/list", opit_show="/api/promotion/{id}"),
    # --- compute (value-add additions) ---
    dict(name="autoscale", domain="compute", tier="read", policy="never",
         opit_list="/api/compute/auto-scale", opit_list_all="/api/compute/auto-scale/all",
         opit_show="/api/compute/auto-scale/{id}"),
    dict(name="gpu", domain="compute", tier="read", policy="never",
         opit_list="/api/compute/gpus", opit_show="/api/compute/gpus/{id}"),
    # GPU instances: OpenStackit splits servers into GPU vs general via the `gpu`
    # query flag on the server list endpoint. server_list (no flag) returns general
    # only; this row exposes the GPU ones. OpenStack fallback filters by the same
    # signal the backend uses: server metadata "opit:device_type" == "gpu"
    # (key is namespaced "opit:"; openstacksdk exposes it under Server.metadata).
    dict(name="gpu_server", domain="compute", tier="read", fields=["id", "name", "status"],
         os_list=lambda c, a: [s for s in c.compute.servers(details=True, all_projects=a)
                               if str((s.metadata or {}).get("opit:device_type", "")).lower() == "gpu"],
         opit_list="/api/compute/servers?gpu=true",
         opit_list_all="/api/compute/servers/list/all?gpu=true"),
    dict(name="pci_device", domain="compute", tier="read", policy="never",
         opit_list="/api/compute/os-pci/list", opit_show="/api/compute/os-pci/{id}"),
    dict(name="snapshot_schedule", domain="compute", tier="read", policy="never",
         opit_list="/api/compute/snapshot-schedule/list", opit_list_all="/api/compute/snapshot-schedule/all",
         opit_show="/api/compute/snapshot-schedule/{id}"),
    dict(name="compute_service", domain="compute", tier="read", policy="never",
         opit_list="/api/compute/os-services"),
    # (identity refresh_token / system_service, image metadef_resource_type,
    #  network qos_rule_type pruned per review; share/manila + swift omitted.)
    # --- observability (reporting; history + alarms pruned per review) ---
    dict(name="report_project", domain="observability", tier="read", policy="never",
         opit_list="/api/reporting/project",
         note="Large dataset (one row per project) — start with limit=N (e.g. 50)."),
    dict(name="report_server", domain="observability", tier="read", policy="never",
         opit_list="/api/reporting/server", opit_show="/api/reporting/server/{id}",
         note="Large dataset (one row per server, cluster-wide) — start with limit=N (e.g. 50); detail=True grows it further."),

    # --- parent-scoped lists (one id → a list; live-probed) --- #
    dict(name="server_action_log", domain="compute", tier="read", policy="never",
         parent_desc="server (UUID)",
         desc="Action history (audit log: past start/stop/reboot/create/resize with timestamps + request IDs)",
         opit_parent_list="/api/compute/servers/{id}/os-instance-actions"),
    dict(name="server_volume_attachment", domain="compute", tier="read", policy="never",
         parent_desc="server (UUID)", desc="List volumes attached to a server",
         opit_parent_list="/api/compute/servers/{id}/os-volume_attachments"),
    dict(name="pool_member", domain="lbaas", tier="read", policy="never",
         parent_desc="load-balancer pool (UUID)", desc="List members of an LB pool",
         opit_parent_list="/api/lbaas/pools/{id}/members"),
    dict(name="user_role", domain="identity", tier="read", policy="never",
         parent_desc="user (id)", desc="List role assignments of a user",
         opit_parent_list="/api/identity/users/{id}/roles"),
    dict(name="user_group", domain="identity", tier="read", policy="never",
         parent_desc="user (id)", desc="List groups a user belongs to",
         opit_parent_list="/api/identity/users/{id}/groups"),
]


def _make_list(spec):
    # policy 'auto' = overlap (OpenStackit-first, OpenStack fallback); 'never' =
    # OpenStackit-only value-add (no os_list on the row, no fallback).
    policy = spec.get("policy", "auto")
    fields = spec.get("fields")

    def _list(ctx: Context, all_projects: bool = False, backend: str = "auto",
              detail: bool = False, limit: int = 0) -> dict:
        conn, sess = _os_conn(ctx), _opit_session(ctx)
        if all_projects and spec.get("opit_list_all"):
            opit_path = spec["opit_list_all"]
        elif all_projects and policy != "never":
            opit_path = None   # overlap w/o an all-projects path → served by OpenStack
        else:
            opit_path = spec["opit_list"]
        opit_key = spec.get("opit_list_key")  # optional: unwrap dict response by this key
        # detail=True 또는 fields 미정의 → 전체 필드. 그 외 → 핵심 컬럼만 투영.
        _proj = (lambda x: _full(x)) if (detail or not fields) else (lambda x: _summary(x, fields))
        # limit>0 → cap rows (large reports overflow context otherwise). 0 = all (default).
        _cap = (lambda xs: list(xs)[:limit]) if (limit and limit > 0) else (lambda xs: xs)

        def _opit_fetch():
            raw = sess.get(opit_path)
            items = raw.get(opit_key, []) if (opit_key and isinstance(raw, dict)) else raw
            return [_proj(x) for x in _cap(items)]

        return route(policy=policy, prefer=_prefer(ctx, backend), openstackit_available=_opit_usable(ctx),
                     openstackit=_opit_fetch if (sess and opit_path) else None,
                     openstack=(lambda: [_proj(x) for x in _cap(spec["os_list"](conn, all_projects))])
                     if (conn and spec.get("os_list")) else None)
    return _list


def _make_show(spec):
    policy = spec.get("policy", "auto")

    def _show(ctx: Context, resource_id: str, backend: str = "auto") -> dict:
        conn, sess = _os_conn(ctx), _opit_session(ctx)
        path = spec["opit_show"].replace("{id}", resource_id)
        return route(policy=policy, prefer=_prefer(ctx, backend), openstackit_available=_opit_usable(ctx),
                     openstackit=(lambda: _full(sess.get(path))) if sess else None,
                     openstack=(lambda: _full(spec["os_show"](conn, resource_id)))
                     if (conn and spec.get("os_show")) else None)
    return _show


def _make_delete(spec):
    """tier=write delete with a human elicitation gate (confirm/cancel, no default).
    The {id} path defaults to the row's opit_show (DELETE method); override with
    spec['opit_delete']. OpenStack fallback via spec['os_delete'](conn, id)."""
    policy = spec.get("policy", "auto")
    name = spec["name"]
    label = name.replace("_", " ")
    opit_tmpl = spec.get("opit_delete") or spec.get("opit_show")

    async def _delete(ctx: Context, resource_id: str, backend: str = "auto") -> dict:
        from pydantic import create_model, Field
        conn, sess = _os_conn(ctx), _opit_session(ctx)
        # best-effort: what is being deleted (don't block delete if lookup fails)
        disp = resource_id
        try:
            if spec.get("opit_show") and sess:
                disp = (sess.get(spec["opit_show"].replace("{id}", resource_id)) or {}).get("name") or resource_id
            elif spec.get("os_show") and conn:
                disp = _full(spec["os_show"](conn, resource_id)).get("name") or resource_id
        except Exception:
            pass
        if _agent_mode(ctx, "delete") == "native":
            # native: 사람 게이트 없이 바로 삭제 (모드 전환 자체가 인가됨)
            opit_path = opit_tmpl.replace("{id}", resource_id) if opit_tmpl else None
            return route(policy=policy, prefer=_prefer(ctx, backend),
                         openstackit_available=_opit_usable(ctx),
                         openstackit=(lambda: (sess.delete(opit_path) or {"deleted": resource_id}))
                         if (sess and opit_path) else None,
                         openstack=(lambda: (spec["os_delete"](conn, resource_id) or {"deleted": resource_id}))
                         if (conn and spec.get("os_delete")) else None)
        # form 모드: 기존 elicitation 확인 게이트 (이하 현행 유지)
        Confirm = create_model("ConfirmDelete", choice=(str, Field(
            ..., description=f"DELETE {label} '{disp}' (id={resource_id})? 되돌릴 수 없음.",
            json_schema_extra={"enum": ["delete", "cancel"],
                               "enumNames": ["삭제 확정", "취소"]})))
        res = await ctx.elicit(message=f"{label} 삭제 확인", schema=Confirm)
        if (getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None
                or res.data.model_dump().get("choice") != "delete"):
            return {"cancelled": True, "type": name, "id": resource_id}
        opit_path = opit_tmpl.replace("{id}", resource_id) if opit_tmpl else None
        return route(policy=policy, prefer=_prefer(ctx, backend),
                     openstackit_available=_opit_usable(ctx),
                     openstackit=(lambda: (sess.delete(opit_path) or {"deleted": resource_id}))
                     if (sess and opit_path) else None,
                     openstack=(lambda: (spec["os_delete"](conn, resource_id) or {"deleted": resource_id}))
                     if (conn and spec.get("os_delete")) else None)
    return _delete


def _make_update(spec):
    """OpenStackit 경로는 read-modify-write 전체 PUT, OpenStack 폴백은 부분. update_fields 만 바디에 실음(넘긴 것만). {id} 경로는
    spec['opit_update'] 또는 opit_show 재사용(PUT). OpenStack 폴백은
    spec['os_update'](conn, id, body_dict)."""
    policy = spec.get("policy", "auto")
    flds = spec["update_fields"]
    opit_tmpl = spec.get("opit_update") or spec.get("opit_show")

    def _update(ctx: Context, resource_id: str, backend: str = "auto", **kwargs) -> dict:
        conn, sess = _os_conn(ctx), _opit_session(ctx)
        body = {k: v for k, v in kwargs.items() if k in flds and v is not None and v != ""}
        if not body:
            raise RuntimeError("수정할 필드를 하나 이상 넘기세요 (넘긴 필드만 변경).")
        opit_path = opit_tmpl.replace("{id}", resource_id) if opit_tmpl else None
        # OpenStackit update는 full-object PUT(replace) — 프론트처럼 read-modify-write.
        # 현재 객체를 raw GET해 변경필드만 overlay 후 전체 PUT (안 보낸 필드 보존 → 클로버/NPE 방지).
        show_tmpl = spec.get("opit_show") or spec.get("opit_update")

        def _opit_rmw():
            cur = sess.get(show_tmpl.replace("{id}", resource_id)) if show_tmpl else None
            merged = {**cur, **body} if isinstance(cur, dict) else dict(body)
            return _full(sess.api("PUT", opit_path, json_body=merged))

        return route(policy=policy, prefer=_prefer(ctx, backend),
                     openstackit_available=_opit_usable(ctx),
                     openstackit=_opit_rmw if (sess and opit_path) else None,
                     openstack=(lambda: _full(spec["os_update"](conn, resource_id, body)))
                     if (conn and spec.get("os_update")) else None)

    # 명명 옵션 인자를 시그니처에 노출 → FastMCP 스키마에 필드가 뜬다.
    P = inspect.Parameter
    params = [P("ctx", P.POSITIONAL_OR_KEYWORD, annotation=Context),
              P("resource_id", P.POSITIONAL_OR_KEYWORD, annotation=str)]
    params += [P(f, P.KEYWORD_ONLY, default=None, annotation=typing.Optional[str]) for f in flds]
    params.append(P("backend", P.KEYWORD_ONLY, default="auto", annotation=str))
    _update.__signature__ = inspect.Signature(params, return_annotation=dict)
    return _update


def _make_parent_list(spec):
    """A list scoped to ONE parent resource: takes the parent id, GETs
    opit_parent_list (single {id}) and returns the list. OpenStackit-only
    (e.g. a server's action log, a pool's members)."""
    policy = spec.get("policy", "never")
    fields = spec.get("fields")

    def _plist(ctx: Context, resource_id: str, backend: str = "auto",
               detail: bool = False) -> dict:
        sess = _opit_session(ctx)
        path = spec["opit_parent_list"].replace("{id}", resource_id)
        _proj = (lambda x: _full(x)) if (detail or not fields) else (lambda x: _summary(x, fields))
        return route(policy=policy, prefer=_prefer(ctx, backend), openstackit_available=_opit_usable(ctx),
                     openstackit=(lambda: [_proj(x) for x in sess.get(path)]) if sess else None,
                     openstack=None)
    return _plist


# Overlap resource → domain (all overlap list/show are 'read' tier).
RESOURCE_DOMAIN = {
    "server": "compute", "flavor": "compute", "keypair": "compute",
    "hypervisor": "compute", "availability_zone": "compute",
    "server_group": "compute", "aggregate": "compute",
    "network": "network", "subnet": "network", "router": "network",
    "port": "network", "security_group": "network",
    "security_group_rule": "network", "floating_ip": "network",
    "qos_policy": "network", "agent": "network", "rbac_policy": "network",
    "network_ip_availability": "network",
    "load_balancer": "lbaas", "listener": "lbaas", "pool": "lbaas",
    "health_monitor": "lbaas", "l7_policy": "lbaas", "lb_flavor": "lbaas",
    "volume": "storage", "volume_snapshot": "storage", "volume_type": "storage",
    "volume_backup": "storage", "qos_spec": "storage", "volume_group": "storage",
    "volume_group_type": "storage", "volume_group_snapshot": "storage",
    "volume_service": "storage",
    "image": "image", "metadef_namespace": "image",
    "project": "identity", "domain": "identity", "role": "identity",
    "user": "identity", "role_assignment": "identity",
    "application_credential": "identity", "region": "identity",
    "service": "identity", "endpoint": "identity",
}

for _spec in RESOURCES:
    _dom = _spec.get("domain") or RESOURCE_DOMAIN[_spec["name"]]
    _tier = _spec.get("tier", "read")
    # Parent-scoped list (one id → a list): e.g. a server's action-history log,
    # a pool's members. Registered as <name>_list(resource_id). OpenStackit-only.
    if _spec.get("opit_parent_list"):
        _pd = _spec.get("parent_desc", "parent")
        _psum = " Key columns only; detail=True for all fields." if _spec.get("fields") else ""
        add(_make_parent_list(_spec), name=f"{_spec['name']}_list", domain=_dom, tier=_tier,
            description=f"{_spec.get('desc', 'List ' + _spec['name'].replace('_', ' ') + 's')} "
                        f"for a {_pd} (pass its id as resource_id).{_psum} OpenStackit-only.")
        continue
    _only = _spec.get("policy") == "never"   # OpenStackit-only value-add
    _scoped = _spec.get("opit_list_all") is not None
    _hint = " all_projects=True for the admin/all view." if _scoped else ""
    _kind = ("OpenStackit-only (no OpenStack equivalent)." if _only
             else "Overlap: OpenStackit-first, OpenStack fallback; backend='openstack'|'openstackit' to force.")
    _note = (" " + _spec["note"]) if _spec.get("note") else ""
    add(_make_list(_spec), name=f"{_spec['name']}_list", domain=_dom, tier=_tier,
        description=f"List {_spec['name'].replace('_', ' ')}s (current project by default).{_hint} "
                    f"Returns key columns only; pass detail=True for all fields (or use {_spec['name']}_show). "
                    f"limit=N caps rows (0=all) — use it for large datasets to stay in context.{_note} {_kind}")
    # Show registers when there's an opit_show AND either an OpenStack show (overlap)
    # or it's an OpenStackit-only resource. Keeps the existing overlap surface intact.
    if _spec.get("opit_show") and (_spec.get("os_show") or _only):
        _sd = (f"Show one {_spec['name'].replace('_', ' ')} by id."
               + (" OpenStackit-only." if _only else " Overlap (OpenStackit-first)."))
        add(_make_show(_spec), name=f"{_spec['name']}_show", domain=_dom, tier=_tier, description=_sd)
    if _spec.get("os_delete") or _spec.get("opit_delete"):
        _dd = (f"Delete one {_spec['name'].replace('_', ' ')} by id. "
               "Asks for human confirmation (confirm/cancel) before deleting — irreversible. "
               + ("OpenStackit-only." if _only else "Overlap (OpenStackit-first)."))
        add(_make_delete(_spec), name=f"{_spec['name']}_delete", domain=_dom, tier="write", description=_dd)
    if _spec.get("update_fields"):
        _ud = (f"Update one {_spec['name'].replace('_', ' ')} by id (부분 업데이트 — 넘긴 필드만 변경). "
               f"Updatable: {', '.join(_spec['update_fields'])}. "
               + ("OpenStackit-only." if _only else "Overlap (OpenStackit-first)."))
        add(_make_update(_spec), name=f"{_spec['name']}_update", domain=_dom, tier="write", description=_ud)


# OpenStackit-only tools (Key Manager, Identity, session) — only in the 'openstackit'
# edition. register() populates the registry via add()/add_prompt().
if HAS_OPIT:
    opit_backend.register(add, add_prompt, route, _opit_session, _opit_usable, _opit_logout)


# --------------------------------------------------------------------------- #
# Instance factory + multimount assembly.
# --------------------------------------------------------------------------- #
def _env_set(var, allowed):
    raw = os.environ.get(var, "")
    if not raw.strip():
        return set(allowed)
    sel = {x.strip() for x in raw.split(",") if x.strip()}
    return sel & set(allowed)


# One-line gist of each domain — the routing map shipped to clients via the
# initialize `instructions` field so agents pick the right mount on the first try.
DOMAIN_GIST = {
    "compute": "servers (+ action log), flavors, keypairs, hypervisors, aggregates, autoscale, gpu, pci, snapshot schedules, capacity",
    "network": "networks, subnets, routers, ports, security groups, floating IPs, agents, RBAC",
    "lbaas": "load balancers, listeners, pools (+ members), health monitors, L7 policies (Octavia)",
    "storage": "block volumes, snapshots, backups, volume types/groups (Cinder)",
    "image": "Glance images, metadata definitions",
    "identity": "projects, users (+ roles/groups), roles, domains, regions, services, endpoints, app credentials (+ create_project/create_user)",
    "keymanager": "Barbican secrets, containers, LB certificates",
    "observability": "logs (log_tail/log_targets — node-local; multi-node via node=: log_targets(node='') lists registered nodes, then log_tail(target, node=<name>) proxies to that node), service_status, reporting (project/server). This base mount reaches every node; per-node log slices exist only as proxy targets — connect to THIS mount, not to them.",
    "billing": "invoices, prices, promotions",
    "orchestration": "Heat stacks, resource types, template versions; Magnum clusters, cluster templates, COE quotas",
}

# Conventions common to EVERY opit-* mount — stated once here, delivered on connect.
_COMMON_INSTRUCTIONS = (
    "⚠️ SCOPE — this server is READ-ONLY except for FOUR write tools: server_stop / "
    "server_start (compute) and create_project / create_user (identity). ANY other state "
    "change — restarting a service (cinder-backup, rabbit, galera), evacuating, resizing, "
    "deleting — is NOT possible here and requires direct host/SSH access. Use this MCP to "
    "DIAGNOSE; do the fix on the host.\n\n"
    "Conventions shared by all opit-* mounts:\n"
    "- Tool names are regular: `<resource>_list` / `<resource>_show` (show needs a UUID). "
    "If your client loads tool schemas on demand, PREDICT the names you need from this pattern "
    "and load them in ONE batch by name — don't fuzzy keyword-search one at a time.\n"
    "- `whoami`, `logout`, `switch_project` exist on EVERY mount (core). whoami's `usable` "
    "is a REAL auth probe (not just credential presence) — trust it on the first call.\n"
    "- Session scope is SHARED across all opit-* mounts: one `switch_project` (or `logout`) "
    "applies everywhere — do NOT re-switch per mount.\n"
    "- `*_list` tools take `all_projects=True` for the admin/all view where supported.\n"
    "- Overlap tools accept `backend='openstack'|'openstackit'`; default is OpenStackit-first "
    "with OpenStack fallback on 5xx/auth errors. OpenStackit-only tools have no fallback.\n"
    "- Every result carries `backend_reason` (primary | fallback | forced | openstack_only) "
    "so you can tell whether a silent OpenStackit→OpenStack fallback happened.\n"
    "- Field VALUES are passed through verbatim from whichever backend served the call, so "
    "casing can differ across backends (e.g. status `ACTIVE` from OpenStack vs `active` from "
    "OpenStackit). When you grep/filter/compare on values like status, match CASE-INSENSITIVELY.\n"
    "- Timestamps are also verbatim: OpenStackit returns epoch MILLISECONDS (e.g. created_at "
    "`1781850485000`), OpenStack returns ISO-8601 strings (e.g. `2026-06-23T02:49:04`). Detect "
    "the type (int vs str) before parsing; the same field can differ by which backend served it.\n"
    "- On failure the error message is `Error executing tool <name>: ` followed by a JSON "
    "object `{\"error\": {\"type\", \"message\", \"http_status\"?, \"attempted_backend\"?}}` — "
    "parse it from the first `{` for the error code and which backend was tried.\n"
    "- service_status covers ONLY Nova/Neutron; for Cinder service health use "
    "volume_service_list (storage).\n"
    "- create and delete each follow a PER-ACTION agent-mode (default `form`: a human "
    "form/confirm via elicitation). Switch with `agent_mode create=native` / `agent_mode "
    "delete=native` to act directly — then create takes `body=`(dict), delete deletes "
    "immediately. delete form→native needs one human confirm (irreversible); create switches "
    "freely. Current modes: `whoami.agent_mode` ({create, delete})."
)


def _instructions(domains):
    """Build the initialize `instructions` text for a mount serving `domains`."""
    mapped = [d for d in DOMAINS if d in domains and d in DOMAIN_GIST]
    if len(mapped) == 1:
        here = f"You are connected to the OpenStackit MCP — **{mapped[0]}** domain ({DOMAIN_GIST[mapped[0]]})."
    else:
        here = "You are connected to the OpenStackit MCP (combined; all domains)."
    routing = "Domain routing map (each domain is its own opit-<domain> mount):\n" + "\n".join(
        f"- {d}: {DOMAIN_GIST[d]}" for d in DOMAINS if d in DOMAIN_GIST)
    # Lead with the READ-ONLY/host-access boundary (the most load-bearing fact), then
    # which mount you're on, the shared conventions, and the routing map.
    return f"{_COMMON_INSTRUCTIONS}\n\n{here}\n\n{routing}"


def _error_json(e: Exception) -> str:
    """Compact structured payload for a failed tool call, so failures carry the
    same machine-readable shape as successes (which are {backend, backend_reason,
    data}). FastMCP hard-prefixes the final message with 'Error executing tool
    <name>: ', so clients parse the JSON from the first '{'."""
    err = {"type": type(e).__name__, "message": str(e)}
    if isinstance(e, OpenStackitError):
        # http_status = the error code; OpenStackitError only arises on the OpenStackit path.
        if getattr(e, "http_status", None) is not None:
            err["http_status"] = e.http_status
        err["attempted_backend"] = "openstackit"
    return json.dumps({"error": err}, ensure_ascii=False)


def _wrap_tool_errors(fn):
    """Convert tool exceptions into a structured JSON message (see _error_json).
    UrlElicitationRequiredError is re-raised untouched — it is the elicitation
    control-flow signal for the create_*_form tools, not an error. CancelledError
    is a BaseException and is never caught here."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*a, **k):
            try:
                return await fn(*a, **k)
            except UrlElicitationRequiredError:
                raise
            except Exception as e:
                raise RuntimeError(_error_json(e)) from e
        return aw

    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except UrlElicitationRequiredError:
            raise
        except Exception as e:
            raise RuntimeError(_error_json(e)) from e
    return w


def make_mcp(name, domains, tiers):
    """Build a FastMCP exposing only tools whose (domain, tier) match. 'core'
    domain tools are always included. The initialize `instructions` carry the
    routing map + shared conventions to every connecting client.

    stateful (stateless_http=False) is REQUIRED for elicitation (the *_form tools):
    server→client requests can only be correlated over a persistent session. Single
    process + --network host means no session-affinity concerns."""
    m = FastMCP(name, transport_security=_TS, stateless_http=False,
                instructions=_instructions(domains))
    for t in _REGISTRY:
        if (t["domain"] == "core" or t["domain"] in domains) and t["tier"] in tiers:
            m.add_tool(_wrap_tool_errors(t["fn"]), name=t["name"], description=t["description"])
    for p in _PROMPTS:
        if p["domain"] == "core" or p["domain"] in domains:
            m.add_prompt(p["prompt"])
    return m


# Which (domain, tier) slices this process serves (env-selectable; default = all).
ACTIVE_DOMAINS = _env_set("OPIT_MCP_DOMAINS", DOMAINS)
ACTIVE_TIERS = _env_set("OPIT_MCP_TIERS", TIERS)

# Combined instance: everything active. Used for stdio + in-process verification +
# the backward-compatible root /mcp mount.
mcp = make_mcp("openstackit", ACTIVE_DOMAINS, ACTIVE_TIERS)

# Per-domain instances, only for domains that actually have tools in the active
# tiers (skips empty domains like billing/orchestration in this phase).
_domain_tools = {t["domain"] for t in _REGISTRY if t["tier"] in ACTIVE_TIERS}
MOUNTS = {d: make_mcp(f"openstackit-{d}", {d}, ACTIVE_TIERS)
          for d in DOMAINS if d in ACTIVE_DOMAINS and d in _domain_tools}


def build_http_app():
    """Parent ASGI app: each active domain served at /<domain>/mcp. The combined
    92-tool instance is NOT exposed over HTTP by default (clients use the per-domain
    endpoints); set OPIT_MCP_COMBINED=1 to also mount it at /mcp. Combines all
    mounted instances' session-manager lifespans."""
    import contextlib
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse
    import ops_backend

    apps = {d: m.streamable_http_app() for d, m in MOUNTS.items()}
    instances = list(MOUNTS.values())

    async def _obs_targets(request):
        return JSONResponse(ops_backend.targets())

    async def _obs_logs(request):
        q = request.query_params
        try:
            lines = int(q.get("lines", "300"))
        except ValueError:
            lines = 300
        try:
            data = ops_backend.tail(q.get("target", ""), lines=lines, grep=q.get("grep", ""),
                                    since=q.get("since", ""), until=q.get("until", ""),
                                    last=q.get("last", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(data)

    async def _obs_trace(request):
        q = request.query_params
        try:
            data = ops_backend.trace(q.get("id", ""), since=q.get("since", ""),
                                     until=q.get("until", ""), last=q.get("last", ""),
                                     targets_csv=q.get("targets", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(data)

    routes = [Route("/obs/targets", _obs_targets),
              Route("/obs/logs", _obs_logs),
              Route("/obs/trace", _obs_trace)] + \
             [Mount(f"/{d}", app=a) for d, a in apps.items()]

    combined = os.environ.get("OPIT_MCP_COMBINED", "").lower() in ("1", "true", "yes")
    if combined:
        instances.append(mcp)
        routes.append(Mount("/", app=mcp.streamable_http_app()))   # /mcp → combined

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for inst in instances:
                await stack.enter_async_context(inst.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=MCP_PORT)
    args = p.parse_args()
    if args.transport == "http":
        import uvicorn
        _combined = os.environ.get("OPIT_MCP_COMBINED", "").lower() in ("1", "true", "yes")
        _paths = ["/" + d + "/mcp" for d in sorted(MOUNTS)] + (["/mcp (combined)"] if _combined else [])
        print(f"mounting domains: {sorted(MOUNTS)} | tiers: {sorted(ACTIVE_TIERS)} | paths: {_paths}")
        uvicorn.run(build_http_app(), host=args.host, port=args.port, log_level="info")
    else:
        mcp.run(transport="stdio")

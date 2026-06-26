"""OpenStack MCP server.

Per-caller credentials AND target endpoint come from request headers (env
fallback for local stdio):
  OpenStack : X-OS-Auth-Url, X-OS-App-Cred-Id, X-OS-App-Cred-Secret

List tools accept all_projects (default False = current project; True =
admin/all-projects view). Tools return their data directly.
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

import os_backend
import ops_backend
from errors import OpenStackError

OS_AUTH_URL = os.environ.get("OS_AUTH_URL", "http://127.0.0.1:5000/v3")

# Port is the single source of truth (MCP_PORT, default 8001; --port overrides at
# launch). The Host-header allowlist is DERIVED from the port so changing the port
# alone doesn't break the transport_security check (set MCP_ALLOWED_HOSTS to override
# the host list, or MCP_ALLOWED_HOST_NAMES to add hostnames beyond the defaults).
MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
_HOST_NAMES = [h.strip() for h in os.environ.get(
    "MCP_ALLOWED_HOST_NAMES", "localhost,127.0.0.1").split(",") if h.strip()]
_DEFAULT_HOSTS = ",".join(f"{h}:{MCP_PORT}" for h in _HOST_NAMES)
_HOSTS = os.environ.get("MCP_ALLOWED_HOSTS", _DEFAULT_HOSTS)

_TS = TransportSecuritySettings(
    allowed_hosts=[h.strip() for h in _HOSTS.split(",") if h.strip()], allowed_origins=["*"])

osp = os_backend.OpenStackProvider(OS_AUTH_URL)

_NO_CREDS = ("no OpenStack credentials — supply X-OS-App-Cred-Id / X-OS-App-Cred-Secret "
             "headers (HTTP) or OS_APPLICATION_CREDENTIAL_ID / _SECRET env (stdio)")


def _os_call(fn):
    """Run an OpenStack op (zero-arg callable) and return its result directly.
    fn is None when credentials are absent → raise a clear error."""
    if fn is None:
        raise RuntimeError(_NO_CREDS)
    return fn()


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


def _os_creds(ctx):
    h = _headers(ctx)
    cid = h.get("x-os-app-cred-id") or os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
    sec = h.get("x-os-app-cred-secret") or os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    return (cid, sec) if cid and sec else None


def _os_conn(ctx):
    creds = _os_creds(ctx)
    return osp.conn(*creds, auth_url=_os_auth_url(ctx)) if creds else None


# cpuinfo = openstacksdk Hypervisor 레거시 별칭(정식 cpu_info와 완전 동일한 거대 객체) → 드롭.
_DROP_KEYS = {"location", "links", "_csrf", "cpuinfo"}


def _full(item):
    """Return ALL meaningful fields of an item (openstacksdk object → to_dict, or
    OpenStack SDK object or plain dict), dropping empty/null values and a little noise.
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
DOMAINS = ["compute", "network", "lbaas", "storage", "image", "identity", "observability"]
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
def whoami(ctx: Context) -> dict:
    """Show whether OpenStack credentials are present and the current project/roles."""
    conn = _os_conn(ctx)
    result = {"openstack": {"auth_url": _os_auth_url(ctx), "credentials": conn is not None}}
    if conn:
        try:
            acc = conn.session.auth.get_access(conn.session)
            result["current_project"] = {"id": conn.current_project_id,
                                         "name": getattr(acc, "project_name", None)}
            result["roles"] = list(getattr(acc, "role_names", []) or [])
        except Exception:
            result["context_note"] = "could not resolve current project/roles"
    return result


def server_stop(ctx: Context, server_id: str) -> dict:
    """Stop (power off) a compute instance."""
    conn = _os_conn(ctx)
    return _os_call((lambda: os_backend.server_stop(conn, server_id)) if conn else None)


def server_start(ctx: Context, server_id: str) -> dict:
    """Start (power on) a compute instance."""
    conn = _os_conn(ctx)
    return _os_call((lambda: os_backend.server_start(conn, server_id)) if conn else None)


def quota_show(ctx: Context, project_id: str = "") -> dict:
    """Project quota + usage (compute / network / block storage)."""
    conn = _os_conn(ctx)
    return _os_call((lambda: os_backend.quota_show(conn, project_id or conn.current_project_id))
                    if conn else None)


def capacity_stats(ctx: Context) -> dict:
    """Aggregate compute capacity vs usage (Placement)."""
    conn = _os_conn(ctx)
    return _os_call((lambda: os_backend.capacity_stats(conn)) if conn else None)


def service_status(ctx: Context) -> dict:
    """Node/service health: ONLY Nova compute services + Neutron network agents
    (up/down). Does NOT cover storage/image — for Cinder service health (e.g.
    cinder-backup/-volume/-scheduler down) use `volume_service_list` in the storage
    domain."""
    conn = _os_conn(ctx)
    return _os_call((lambda: os_backend.service_status(conn)) if conn else None)


def log_targets(ctx: Context) -> dict:
    """List available Kolla log targets (per-service log dirs)."""
    return ops_backend.targets()


def log_tail(ctx: Context, target: str, lines: int = 300, grep: str = "",
             since: str = "", until: str = "", last: str = "") -> dict:
    """한 target 로그를 시간창 안에서 구조화·시간순으로 반환. since/until(절대 '2026-06-25 14:30'
    또는 '14:30') 또는 last('30m'/'2h'/'1d'); 모두 비면 최근 30분. grep=정규식 필터.
    결과의 cursor를 다음 호출 since로 주면 새 줄만(폴링).
    target 예: 'kolla:nova'."""
    return ops_backend.tail(target, lines=lines, grep=grep, since=since, until=until, last=last)


def log_trace(ctx: Context, id: str, since: str = "", until: str = "", last: str = "",
              targets: str = "") -> dict:
    """한 요청 ID(OpenStack 'req-...')가 거쳐간 로그를 여러 서비스에서 모아 시간순으로 반환.
    targets: 'kolla:nova,kolla:neutron' 형식의 쉼표 구분 목록으로 검색 범위를 제한 (기본값: 전체)."""
    return ops_backend.trace(id, since=since, until=until, last=last, targets_csv=targets)


add(whoami, name="whoami", domain="core", tier="read",
    description=whoami.__doc__)
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
# Resources — list/show/delete/update generated from a declarative table.
#   os_list: fn(conn, all_projects)   os_show/os_update/os_delete: SDK callables
# Default all_projects=False → scope to the current project.
# --------------------------------------------------------------------------- #
RESOURCES = [
    dict(name="server", fields=["id", "name", "status"],
         os_list=_nova("servers"),
         os_show=lambda c, i: c.compute.get_server(c.compute.find_server(i, ignore_missing=False).id),
         os_update=lambda c, i, b: c.compute.update_server(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.compute.delete_server(i, ignore_missing=False)),
    dict(name="flavor", fields=["id", "name", "vcpus", "ram", "disk"],
         os_list=_plain(lambda c: c.compute.flavors()),
         os_show=lambda c, i: c.compute.get_flavor(i),
         os_delete=lambda c, i: c.compute.delete_flavor(i, ignore_missing=False)),
    dict(name="keypair", fields=["name", "fingerprint", "type"],
         os_list=_plain(lambda c: c.compute.keypairs()),
         os_delete=lambda c, i: c.compute.delete_keypair(i, ignore_missing=False)),
    dict(name="hypervisor", fields=["id", "name", "status", "state", "hypervisor_type", "hypervisor_hostname"],
         os_list=_plain(lambda c: c.compute.hypervisors(details=True))),
    dict(name="availability_zone", fields=["name", "state"],
         os_list=_plain(lambda c: c.compute.availability_zones())),
    dict(name="network", fields=["id", "name", "status", "is_router_external"],
         os_list=_neutron("networks"),
         os_show=lambda c, i: c.network.get_network(i),
         os_delete=lambda c, i: c.network.delete_network(i, ignore_missing=False),
         os_update=lambda c, i, b: c.network.update_network(i, **b),
         update_fields=["name", "description"]),
    dict(name="subnet", fields=["id", "name", "cidr", "ip_version", "network_id"],
         os_list=_neutron("subnets"),
         os_show=lambda c, i: c.network.get_subnet(i),
         os_update=lambda c, i, b: c.network.update_subnet(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_subnet(i, ignore_missing=False)),
    dict(name="router", fields=["id", "name", "status"],
         os_list=_neutron("routers"),
         os_show=lambda c, i: c.network.get_router(i),
         os_update=lambda c, i, b: c.network.update_router(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_router(i, ignore_missing=False)),
    dict(name="port", fields=["id", "name", "status", "mac_address", "network_id"],
         os_list=_neutron("ports"),
         os_show=lambda c, i: c.network.get_port(i),
         os_update=lambda c, i, b: c.network.update_port(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_port(i, ignore_missing=False)),
    dict(name="security_group", fields=["id", "name", "description"],
         os_list=_neutron("security_groups"),
         os_show=lambda c, i: c.network.get_security_group(i),
         os_update=lambda c, i, b: c.network.update_security_group(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.network.delete_security_group(i, ignore_missing=False)),
    dict(name="security_group_rule",
         fields=["id", "direction", "protocol", "port_range_min", "port_range_max", "ethertype"],
         os_list=_neutron("security_group_rules"),
         os_delete=lambda c, i: c.network.delete_security_group_rule(i, ignore_missing=False)),
    dict(name="floating_ip", fields=["id", "floating_ip_address", "fixed_ip_address", "status", "port_id"],
         os_list=_neutron("ips"),
         os_show=lambda c, i: c.network.get_ip(i),
         os_update=lambda c, i, b: c.network.update_ip(i, **b),
         update_fields=["description", "port_id"],
         os_delete=lambda c, i: c.network.delete_ip(i, ignore_missing=False)),
    dict(name="volume", fields=["id", "name", "status", "size"],
         os_list=_cinder("volumes"),
         os_show=lambda c, i: c.block_storage.get_volume(i),
         os_update=lambda c, i, b: c.block_storage.update_volume(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.block_storage.delete_volume(i, ignore_missing=False)),
    dict(name="volume_snapshot", fields=["id", "name", "status", "size", "volume_id"],
         os_list=_cinder("snapshots"),
         os_show=lambda c, i: c.block_storage.get_snapshot(i),
         os_delete=lambda c, i: c.block_storage.delete_snapshot(i, ignore_missing=False)),
    dict(name="volume_type", fields=["id", "name", "is_public"],
         os_list=_plain(lambda c: c.block_storage.types())),
    dict(name="volume_backup", fields=["id", "name", "status", "size"],
         os_list=_cinder("backups"),
         os_show=lambda c, i: c.block_storage.get_backup(i),
         os_delete=lambda c, i: c.block_storage.delete_backup(i, ignore_missing=False)),
    dict(name="image", fields=["id", "name", "status", "disk_format", "visibility", "size"],
         os_list=lambda c, a: (c.image.images() if a else c.image.images(owner=c.current_project_id)),
         os_show=lambda c, i: c.image.get_image(i),
         os_delete=lambda c, i: c.image.delete_image(i, ignore_missing=False)),
    dict(name="project", fields=["id", "name", "description"],
         os_list=_plain(lambda c: c.identity.projects()),
         os_show=lambda c, i: c.identity.get_project(i),
         os_delete=lambda c, i: c.identity.delete_project(i, ignore_missing=False)),
    dict(name="domain", fields=["id", "name", "description"],
         os_list=_plain(lambda c: c.identity.domains()),
         os_show=lambda c, i: c.identity.get_domain(i),
         os_delete=lambda c, i: c.identity.delete_domain(i, ignore_missing=False)),
    dict(name="load_balancer", fields=["id", "name", "provisioning_status", "operating_status", "vip_address"],
         os_list=_octavia("load_balancers"),
         os_show=lambda c, i: c.load_balancer.get_load_balancer(i),
         os_update=lambda c, i, b: c.load_balancer.update_load_balancer(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_load_balancer(i, ignore_missing=False)),
    dict(name="listener", fields=["id", "name", "protocol", "protocol_port"],
         os_list=_octavia("listeners"),
         os_show=lambda c, i: c.load_balancer.get_listener(i),
         os_update=lambda c, i, b: c.load_balancer.update_listener(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_listener(i, ignore_missing=False)),
    dict(name="pool", fields=["id", "name", "protocol", "lb_algorithm"],
         os_list=_octavia("pools"),
         os_show=lambda c, i: c.load_balancer.get_pool(i),
         os_update=lambda c, i, b: c.load_balancer.update_pool(i, **b),
         update_fields=["name", "description"],
         os_delete=lambda c, i: c.load_balancer.delete_pool(i, ignore_missing=False)),
    dict(name="role", fields=["id", "name", "domain_id", "description"],
         os_list=_plain(lambda c: c.identity.roles()),
         os_delete=lambda c, i: c.identity.delete_role(i, ignore_missing=False)),
    # --- TIER 1: Identity ---
    dict(name="user", fields=["id", "name", "email", "is_enabled", "domain_id"],
         os_list=_plain(lambda c: c.identity.users()),
         os_show=lambda c, i: c.identity.get_user(i),
         os_update=lambda c, i, b: c.identity.update_user(i, **b),
         update_fields=["name", "email"],
         os_delete=lambda c, i: c.identity.delete_user(i, ignore_missing=False)),
    # role_assignment: admin-scoped, no show endpoint. SDK emits nested dicts
    # (role/scope/user/group) with no flat per-field attributes; use those directly.
    dict(name="role_assignment",
         fields=["role", "scope", "user", "group"],
         os_list=_plain(lambda c: c.identity.role_assignments())),
    # application_credential: scoped to current user on SDK side
    dict(name="application_credential",
         fields=["id", "name", "description", "expires_at"],
         os_list=_plain(lambda c: c.identity.application_credentials(c.current_user_id))),
    # region: globally readable
    dict(name="region", fields=["id", "description", "parent_region_id"],
         os_list=_plain(lambda c: c.identity.regions()),
         os_show=lambda c, i: c.identity.get_region(i)),
    # service: admin-scoped catalog service entries
    dict(name="service", fields=["id", "name", "type", "description", "enabled"],
         os_list=_plain(lambda c: c.identity.services()),
         os_show=lambda c, i: c.identity.get_service(i)),
    # endpoint: admin-scoped catalog endpoints
    dict(name="endpoint",
         fields=["id", "service_id", "interface", "region", "url", "enabled"],
         os_list=_plain(lambda c: c.identity.endpoints()),
         os_show=lambda c, i: c.identity.get_endpoint(i)),
    # --- TIER 2: Networking ---
    # agent: admin/global neutron agents
    dict(name="agent", fields=["id", "agent_type", "binary", "host", "alive", "admin_state_up"],
         os_list=_plain(lambda c: c.network.agents()),
         os_show=lambda c, i: c.network.get_agent(i)),
    # rbac_policy: project-scoped RBAC policies
    dict(name="rbac_policy", fields=["id", "object_type", "object_id", "action", "target_tenant", "project_id"],
         os_list=_neutron("rbac_policies"),
         os_show=lambda c, i: c.network.get_rbac_policy(i)),
    # network_ip_availability: admin. identifier is network_id (not a plain UUID field named id)
    dict(name="network_ip_availability",
         fields=["network_id", "network_name", "total_ips", "used_ips", "subnet_ip_availability"],
         os_list=_plain(lambda c: c.network.network_ip_availabilities()),
         os_show=lambda c, i: c.network.get_network_ip_availability(i)),
    # --- TIER 2: LBaaS ---
    # health_monitor: project-scoped
    dict(name="health_monitor", fields=["id", "name", "type", "delay", "timeout", "max_retries", "operating_status"],
         os_list=_octavia("health_monitors"),
         os_show=lambda c, i: c.load_balancer.get_health_monitor(i),
         os_update=lambda c, i, b: c.load_balancer.update_health_monitor(i, **b),
         update_fields=["name"],
         os_delete=lambda c, i: c.load_balancer.delete_health_monitor(i, ignore_missing=False)),
    # l7_policy: project-scoped
    dict(name="l7_policy", fields=["id", "name", "action", "listener_id", "position", "provisioning_status"],
         os_list=_octavia("l7_policies"),
         os_show=lambda c, i: c.load_balancer.get_l7_policy(i),
         os_delete=lambda c, i: c.load_balancer.delete_l7_policy(i, ignore_missing=False)),
    # lb_flavor: global (not project-scoped) octavia flavor — distinct from compute flavor
    dict(name="lb_flavor", fields=["id", "name", "description", "flavor_profile_id", "enabled"],
         os_list=_plain(lambda c: c.load_balancer.flavors()),
         os_show=lambda c, i: c.load_balancer.get_flavor(i)),
    # --- TIER 2: Compute ---
    # aggregate: admin-global host aggregates
    dict(name="aggregate", fields=["id", "name", "availability_zone", "hosts", "metadata"],
         os_list=_plain(lambda c: c.compute.aggregates()),
         os_show=lambda c, i: c.compute.get_aggregate(i),
         os_update=lambda c, i, b: c.compute.update_aggregate(i, **b),
         update_fields=["name"],
         os_delete=lambda c, i: c.compute.delete_aggregate(i, ignore_missing=False)),
    # server_group: admin-global affinity/anti-affinity groups
    dict(name="server_group", fields=["id", "name", "policy", "policies", "members"],
         os_list=_plain(lambda c: c.compute.server_groups()),
         os_show=lambda c, i: c.compute.get_server_group(i),
         os_delete=lambda c, i: c.compute.delete_server_group(i, ignore_missing=False)),
    # --- TIER 2: Block Storage ---
    # volume_group: project-scoped volume groups
    dict(name="volume_group", fields=["id", "name", "status", "group_type", "availability_zone"],
         os_list=_cinder("groups"),
         os_show=lambda c, i: c.block_storage.get_group(i)),
    # volume_group_type: global group types
    dict(name="volume_group_type", fields=["id", "name", "description", "is_public", "group_specs"],
         os_list=_plain(lambda c: c.block_storage.group_types()),
         os_show=lambda c, i: c.block_storage.get_group_type(i)),
    # volume_group_snapshot: project-scoped group snapshots
    dict(name="volume_group_snapshot", fields=["id", "name", "status", "group_id", "group_type_id"],
         os_list=_cinder("group_snapshots"),
         os_show=lambda c, i: c.block_storage.get_group_snapshot(i)),
    # volume_service: admin-scoped cinder services. no show endpoint.
    dict(name="volume_service", fields=["binary", "host", "state", "status", "zone", "updated_at"],
         os_list=_plain(lambda c: c.block_storage.services())),
    # --- TIER 2: Image ---
    # metadef_namespace: glance metadata definition namespaces.
    # identifier is namespace name (string), not UUID.
    dict(name="metadef_namespace",
         fields=["namespace", "display_name", "description", "visibility", "protected", "owner"],
         os_list=_plain(lambda c: c.image.metadef_namespaces()),
         os_show=lambda c, i: c.image.get_metadef_namespace(i)),
]


def _make_list(spec):
    fields = spec.get("fields")

    def _list(ctx: Context, all_projects: bool = False,
              detail: bool = False, limit: int = 0) -> dict:
        conn = _os_conn(ctx)
        _proj = (lambda x: _full(x)) if (detail or not fields) else (lambda x: _summary(x, fields))
        _cap = (lambda xs: list(xs)[:limit]) if (limit and limit > 0) else (lambda xs: xs)
        return _os_call(
            (lambda: [_proj(x) for x in _cap(spec["os_list"](conn, all_projects))])
            if (conn and spec.get("os_list")) else None)
    return _list


def _make_show(spec):
    def _show(ctx: Context, resource_id: str) -> dict:
        conn = _os_conn(ctx)
        return _os_call((lambda: _full(spec["os_show"](conn, resource_id)))
                        if (conn and spec.get("os_show")) else None)
    return _show


def _make_delete(spec):
    """tier=write delete with a human elicitation gate (confirm/cancel, no default).
    Executes spec['os_delete'](conn, id) after confirmation."""
    name = spec["name"]
    label = name.replace("_", " ")

    async def _delete(ctx: Context, resource_id: str) -> dict:
        from pydantic import create_model, Field
        conn = _os_conn(ctx)
        disp = resource_id
        try:
            if spec.get("os_show") and conn:
                disp = _full(spec["os_show"](conn, resource_id)).get("name") or resource_id
        except Exception:
            pass
        Confirm = create_model("ConfirmDelete", choice=(str, Field(
            ..., description=f"DELETE {label} '{disp}' (id={resource_id})? 되돌릴 수 없음.",
            json_schema_extra={"enum": ["delete", "cancel"],
                               "enumNames": ["삭제 확정", "취소"]})))
        res = await ctx.elicit(message=f"{label} 삭제 확인", schema=Confirm)
        if (getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None
                or res.data.model_dump().get("choice") != "delete"):
            return {"cancelled": True, "type": name, "id": resource_id}
        return _os_call((lambda: (spec["os_delete"](conn, resource_id) or {"deleted": resource_id}))
                        if (conn and spec.get("os_delete")) else None)
    return _delete


def _make_update(spec):
    """Partial update: update_fields 만 바디에 실음(넘긴 것만).
    Executes spec['os_update'](conn, id, body_dict)."""
    flds = spec["update_fields"]

    def _update(ctx: Context, resource_id: str, **kwargs) -> dict:
        conn = _os_conn(ctx)
        body = {k: v for k, v in kwargs.items() if k in flds and v is not None and v != ""}
        if not body:
            raise RuntimeError("수정할 필드를 하나 이상 넘기세요 (넘긴 필드만 변경).")
        return _os_call((lambda: _full(spec["os_update"](conn, resource_id, body)))
                        if (conn and spec.get("os_update")) else None)

    # 명명 옵션 인자를 시그니처에 노출 → FastMCP 스키마에 필드가 뜬다.
    P = inspect.Parameter
    params = [P("ctx", P.POSITIONAL_OR_KEYWORD, annotation=Context),
              P("resource_id", P.POSITIONAL_OR_KEYWORD, annotation=str)]
    params += [P(f, P.KEYWORD_ONLY, default=None, annotation=typing.Optional[str]) for f in flds]
    _update.__signature__ = inspect.Signature(params, return_annotation=dict)
    return _update


# Resource → domain (all generated list/show are 'read' tier).
RESOURCE_DOMAIN = {
    "server": "compute", "flavor": "compute", "keypair": "compute",
    "hypervisor": "compute", "availability_zone": "compute",
    "server_group": "compute", "aggregate": "compute",
    "network": "network", "subnet": "network", "router": "network",
    "port": "network", "security_group": "network",
    "security_group_rule": "network", "floating_ip": "network",
    "agent": "network", "rbac_policy": "network",
    "network_ip_availability": "network",
    "load_balancer": "lbaas", "listener": "lbaas", "pool": "lbaas",
    "health_monitor": "lbaas", "l7_policy": "lbaas", "lb_flavor": "lbaas",
    "volume": "storage", "volume_snapshot": "storage", "volume_type": "storage",
    "volume_backup": "storage", "volume_group": "storage",
    "volume_group_type": "storage", "volume_group_snapshot": "storage",
    "volume_service": "storage",
    "image": "image", "metadef_namespace": "image",
    "project": "identity", "domain": "identity", "role": "identity",
    "user": "identity", "role_assignment": "identity",
    "application_credential": "identity", "region": "identity",
    "service": "identity", "endpoint": "identity",
}

# Resources whose os_list takes an all_projects flag (nova/cinder native, or
# neutron/octavia project-scope helpers). Used only to hint all_projects in the
# tool description.
_ALL_PROJECTS_OK = {
    "server", "network", "subnet", "router", "port", "security_group",
    "security_group_rule", "floating_ip", "volume", "volume_snapshot",
    "volume_backup", "image", "load_balancer", "listener", "pool",
    "health_monitor", "l7_policy", "rbac_policy", "volume_group",
    "volume_group_snapshot",
}

for _spec in RESOURCES:
    _dom = _spec.get("domain") or RESOURCE_DOMAIN[_spec["name"]]
    _tier = _spec.get("tier", "read")
    _scoped = _spec.get("os_list") and _spec["name"] in _ALL_PROJECTS_OK
    _hint = " all_projects=True for the admin/all view." if _scoped else ""
    add(_make_list(_spec), name=f"{_spec['name']}_list", domain=_dom, tier=_tier,
        description=f"List {_spec['name'].replace('_', ' ')}s (current project by default).{_hint} "
                    f"Returns key columns only; pass detail=True for all fields (or use {_spec['name']}_show). "
                    f"limit=N caps rows (0=all).")
    if _spec.get("os_show"):
        add(_make_show(_spec), name=f"{_spec['name']}_show", domain=_dom, tier=_tier,
            description=f"Show one {_spec['name'].replace('_', ' ')} by id.")
    if _spec.get("os_delete"):
        add(_make_delete(_spec), name=f"{_spec['name']}_delete", domain=_dom, tier="write",
            description=f"Delete one {_spec['name'].replace('_', ' ')} by id. "
                        "Asks for human confirmation (confirm/cancel) before deleting — irreversible.")
    if _spec.get("update_fields"):
        add(_make_update(_spec), name=f"{_spec['name']}_update", domain=_dom, tier="write",
            description=f"Update one {_spec['name'].replace('_', ' ')} by id (부분 업데이트 — 넘긴 필드만 변경). "
                        f"Updatable: {', '.join(_spec['update_fields'])}.")


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
    "compute": "servers, flavors, keypairs, hypervisors, aggregates, server groups, capacity",
    "network": "networks, subnets, routers, ports, security groups, floating IPs, agents, RBAC",
    "lbaas": "load balancers, listeners, pools, health monitors, L7 policies (Octavia)",
    "storage": "block volumes, snapshots, backups, volume types/groups (Cinder)",
    "image": "Glance images, metadata definitions",
    "identity": "projects, users, roles, domains, regions, services, endpoints, app credentials",
    "observability": "Kolla service logs (log_tail/log_targets), service_status, capacity",
}

# Conventions common to EVERY mount — stated once here, delivered on connect.
_COMMON_INSTRUCTIONS = (
    "⚠️ SCOPE — this server is READ-ONLY except for write tools (server_start/stop, "
    "and *_update / *_delete where exposed). Restarting host services, evacuating, or "
    "resizing requires direct host access. Use this MCP to inspect and make targeted changes.\n\n"
    "Conventions:\n"
    "- Tool names are regular: `<resource>_list` / `<resource>_show` (show needs an id).\n"
    "- Credentials are per-caller: pass a Keystone application credential via "
    "X-OS-App-Cred-Id / X-OS-App-Cred-Secret headers (HTTP), or OS_APPLICATION_CREDENTIAL_ID / "
    "_SECRET env (stdio). The server stores nothing.\n"
    "- `*_list` returns key columns; pass detail=True for all fields, limit=N to cap rows, "
    "all_projects=True for the admin/all view where supported.\n"
    "- *_delete asks for a human confirmation before deleting (irreversible).\n"
    "- On failure the error message is `Error executing tool <name>: ` followed by a JSON "
    "object `{\"error\": {\"type\", \"message\", \"http_status\"?}}` — parse from the first `{`.\n"
    "- service_status covers Nova/Neutron; for Cinder service health use volume_service_list."
)


def _instructions(domains):
    """Build the initialize `instructions` text for a mount serving `domains`."""
    mapped = [d for d in DOMAINS if d in domains and d in DOMAIN_GIST]
    if len(mapped) == 1:
        here = f"You are connected to the OpenStack MCP — **{mapped[0]}** domain ({DOMAIN_GIST[mapped[0]]})."
    else:
        here = "You are connected to the OpenStack MCP (combined; all domains)."
    routing = "Domain routing map (each domain is its own openstack-<domain> mount):\n" + "\n".join(
        f"- {d}: {DOMAIN_GIST[d]}" for d in DOMAINS if d in DOMAIN_GIST)
    # Lead with the READ-ONLY/host-access boundary (the most load-bearing fact), then
    # which mount you're on, the shared conventions, and the routing map.
    return f"{_COMMON_INSTRUCTIONS}\n\n{here}\n\n{routing}"


def _error_json(e: Exception) -> str:
    err = {"type": type(e).__name__, "message": str(e)}
    if isinstance(e, OpenStackError) and getattr(e, "http_status", None) is not None:
        err["http_status"] = e.http_status
    return json.dumps({"error": err}, ensure_ascii=False)


def _wrap_tool_errors(fn):
    """Wrap a tool function so that any Exception is converted into the
    structured ``{"error": {"type", "message", "http_status?"}}`` JSON message
    (via _error_json) raised as RuntimeError. CancelledError is a BaseException
    and is never caught here."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception as e:
                raise RuntimeError(_error_json(e)) from e
        return aw

    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            raise RuntimeError(_error_json(e)) from e
    return w


def make_mcp(name, domains, tiers):
    """Build a FastMCP exposing only tools whose (domain, tier) match. 'core'
    domain tools are always included. The initialize `instructions` carry the
    routing map + shared conventions to every connecting client.

    stateful (stateless_http=False) is REQUIRED for elicitation (the *_delete
    confirmation): server→client requests can only be correlated over a persistent
    session. Single process + --network host means no session-affinity concerns."""
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
ACTIVE_DOMAINS = _env_set("OSMCP_DOMAINS", DOMAINS)
ACTIVE_TIERS = _env_set("OSMCP_TIERS", TIERS)

# Combined instance: everything active. Used for stdio + in-process verification +
# the backward-compatible root /mcp mount.
mcp = make_mcp("openstack", ACTIVE_DOMAINS, ACTIVE_TIERS)

# Per-domain instances, only for domains that actually have tools in the active
# tiers (skips empty domains in this phase).
_domain_tools = {t["domain"] for t in _REGISTRY if t["tier"] in ACTIVE_TIERS}
MOUNTS = {d: make_mcp(f"openstack-{d}", {d}, ACTIVE_TIERS)
          for d in DOMAINS if d in ACTIVE_DOMAINS and d in _domain_tools}


def build_http_app():
    """Parent ASGI app: each active domain served at /<domain>/mcp. The combined
    instance is NOT exposed over HTTP by default (clients use the per-domain
    endpoints); set OSMCP_COMBINED=1 to also mount it at /mcp. Combines all
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

    combined = os.environ.get("OSMCP_COMBINED", "").lower() in ("1", "true", "yes")
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
        _combined = os.environ.get("OSMCP_COMBINED", "").lower() in ("1", "true", "yes")
        _paths = ["/" + d + "/mcp" for d in sorted(MOUNTS)] + (["/mcp (combined)"] if _combined else [])
        print(f"mounting domains: {sorted(MOUNTS)} | tiers: {sorted(ACTIVE_TIERS)} | paths: {_paths}")
        uvicorn.run(build_http_app(), host=args.host, port=args.port, log_level="info")
    else:
        mcp.run(transport="stdio")

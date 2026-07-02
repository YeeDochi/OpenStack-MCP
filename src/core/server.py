"""OpenStack MCP — standalone, OpenStack-only. Self-complete server; the
management layer builds ON TOP of this (imports it), never the reverse."""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import Context

from core.context import os_conn, os_auth_url
from core.factories import make_delete, make_update, _envelope
from core.registry import Registry, register_resources, DOMAINS, TIERS
from core.specs import CORE_SPECS
from core.assembly import make_mcp, build_http_app, env_set, MCP_PORT
from core import os_backend
from core import observability as obs


def whoami(ctx: Context) -> dict:
    """Show whether the caller's OpenStack backend is configured/reachable."""
    conn = os_conn(ctx)
    result = {"edition": "openstack",
              "openstack": {"auth_url": os_auth_url(ctx), "configured": conn is not None}}
    if conn is not None:
        try:
            acc = conn.session.auth.get_access(conn.session)
            result["openstack"]["current_project"] = {
                "id": conn.current_project_id,
                "name": getattr(acc, "project_name", None)}
            result["openstack"]["roles"] = list(getattr(acc, "role_names", []) or [])
        except Exception as e:
            result["openstack"]["reachable"] = False
            result["openstack"]["reason"] = type(e).__name__
    return result


def register_core_tools(reg):
    reg.add(whoami, name="whoami", domain="core", tier="read",
            description="Show which OpenStack backend is configured/reachable for the caller.")


def service_status(ctx: Context) -> dict:
    """Node/service health: ONLY Nova compute services + Neutron network agents
    (up/down). Does NOT cover storage/image — for Cinder service health use
    `volume_service_list` in the storage domain."""
    conn = os_conn(ctx)
    if conn is None:
        raise ValueError("OpenStack backend not configured for this caller")
    return _envelope(os_backend.service_status(conn))


def log_targets(ctx: Context, node: str = "") -> dict:
    """List log targets. node='' → known node list + this (base) node's targets;
    node='<name>' → that node's targets (proxied). Call with node='' first to see
    which nodes are registered (LOG_NODES). Kolla host log files."""
    return obs.targets_for(node)


def log_tail(ctx: Context, target: str, lines: int = 300, grep: str = "",
             node: str = "", since: str = "", until: str = "", last: str = "") -> dict:
    """한 target 로그를 시간창 안에서 구조화·시간순으로 반환. since/until(절대 '2026-06-25 14:30'
    또는 '14:30') 또는 last('30m'/'2h'/'1d'); 모두 비면 최근 30분. grep=정규식 필터, node=다른 노드.
    결과의 cursor를 다음 호출 since로 주면 새 줄만(폴링).
    target 예: 'kolla:nova' 또는 'kolla:nova/nova-api.log'."""
    return obs.tail_for(target, lines=lines, grep=grep, node=node,
                        since=since, until=until, last=last)


def log_trace(ctx: Context, id: str, since: str = "", until: str = "", last: str = "",
              nodes: str = "", targets: str = "", link_by: str = "none") -> dict:
    """한 요청 ID('req-...')가 거쳐간 로그를 여러 서비스·노드에서 모아 시간순으로 돌려준다.
    nodes=''(로컬)/'all'(등록 노드 전체)/'c1,c2'. targets로 서비스 한정. 시간창은 since/until
    또는 last(기본 30m). cursor로 폴링.

    link_by='entity'면 1차 결과에서 instance/server UUID와 공출현 req- id를 자동 추출해 그
    ID들로 한 번 더 스윕(창은 seed 구간 ±15s로 좁힘) → 여러 서비스에 걸친 한 작업의 전 여정이
    한 타임라인에 엮인다. 결과의 linked_ids로 무엇이 결합됐는지 확인. 기본 'none'은 단일 ID만."""
    return obs.trace_for(id, since=since, until=until, last=last,
                         nodes=nodes, targets_csv=targets, link_by=link_by)


def server_stop(ctx: Context, server_id: str) -> dict:
    """Stop (power off) a compute instance. OpenStack backend."""
    conn = os_conn(ctx)
    if conn is None:
        raise RuntimeError("no OpenStack credentials")
    return _envelope(os_backend.server_stop(conn, server_id))


def server_start(ctx: Context, server_id: str) -> dict:
    """Start (power on) a compute instance. OpenStack backend."""
    conn = os_conn(ctx)
    if conn is None:
        raise RuntimeError("no OpenStack credentials")
    return _envelope(os_backend.server_start(conn, server_id))


def quota_show(ctx: Context, project_id: str = "") -> dict:
    """Project quota + usage across compute / network / block storage, via the
    OpenStack SDK. Defaults to YOUR current project; pass project_id for another
    project. OpenStack backend."""
    conn = os_conn(ctx)
    if conn is None:
        raise RuntimeError("no OpenStack credentials")
    return _envelope(os_backend.quota_show(conn, project_id or conn.current_project_id))


def capacity_stats(ctx: Context) -> dict:
    """Aggregate compute capacity vs usage (Placement). OpenStack backend."""
    conn = os_conn(ctx)
    if conn is None:
        raise RuntimeError("no OpenStack credentials")
    return _envelope(os_backend.capacity_stats(conn))


def register_compute_handtools(reg):
    reg.add(server_stop, name="server_stop", domain="compute", tier="write",
            description=server_stop.__doc__)
    reg.add(server_start, name="server_start", domain="compute", tier="write",
            description=server_start.__doc__)
    reg.add(quota_show, name="quota_show", domain="compute", tier="read",
            description=quota_show.__doc__)
    reg.add(capacity_stats, name="capacity_stats", domain="compute", tier="read",
            description=capacity_stats.__doc__)


def register_observability(reg):
    reg.add(service_status, name="service_status", domain="observability", tier="read",
            description=service_status.__doc__)
    reg.add(log_targets, name="log_targets", domain="observability", tier="read",
            description=log_targets.__doc__)
    reg.add(log_tail, name="log_tail", domain="observability", tier="read",
            description=log_tail.__doc__)
    reg.add(log_trace, name="log_trace", domain="observability", tier="read",
            description=log_trace.__doc__)


def build_registry() -> Registry:
    reg = Registry()
    register_resources(reg, CORE_SPECS,     # os-only factories (default)
                       make_delete=make_delete, make_update=make_update)
    register_core_tools(reg)
    register_compute_handtools(reg)
    register_observability(reg)
    return reg


def core_main():
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=MCP_PORT)
    args = p.parse_args()
    reg = build_registry()
    if args.transport == "http":
        import uvicorn
        app, mounted = build_http_app(reg)
        print(f"mounting domains: {mounted}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        active = env_set("MCP_DOMAINS", DOMAINS)
        make_mcp(reg, "openstack", active, env_set("MCP_TIERS", TIERS)).run(transport="stdio")


if __name__ == "__main__":
    core_main()

"""Box A — OpenStack backend (always shipped).

OpenStackProvider (openstacksdk connection from a Keystone application
credential) plus the OpenStack implementation of each operation. These are plain
functions taking a connection; server.py wires them into MCP tools via the router.
"""
from __future__ import annotations

import hashlib
from typing import Any

import openstack


class OpenStackProvider:
    """Caches connections keyed by (credential, auth_url). The auth_url is passed
    per call so one provider can serve multiple Keystone endpoints."""

    def __init__(self, default_auth_url: str):
        self.default_auth_url = default_auth_url
        self._conns: dict[str, openstack.connection.Connection] = {}

    def conn(self, cred_id: str, cred_secret: str,
             auth_url: str | None = None) -> openstack.connection.Connection:
        auth_url = auth_url or self.default_auth_url
        key = hashlib.sha256(f"{cred_id}\0{cred_secret}\0{auth_url}".encode()).hexdigest()
        c = self._conns.get(key)
        if c is None:
            c = openstack.connection.Connection(
                auth_url=auth_url,
                auth_type="v3applicationcredential",
                application_credential_id=cred_id,
                application_credential_secret=cred_secret,
            )
            self._conns[key] = c
        return c


# --- serializers ----------------------------------------------------------- #
def _server(s: Any) -> dict:
    addrs = {}
    for net, items in (s.addresses or {}).items():
        addrs[net] = [i.get("addr") for i in items if i.get("addr")]
    return {"id": s.id, "name": s.name, "status": s.status,
            "flavor": (s.flavor or {}).get("original_name") or (s.flavor or {}).get("id"),
            "addresses": addrs, "created_at": getattr(s, "created_at", None)}


def _network(n: Any) -> dict:
    return {"id": n.id, "name": n.name, "status": n.status,
            "is_external": getattr(n, "is_router_external", None)}


def _volume(v: Any) -> dict:
    return {"id": v.id, "name": v.name, "status": v.status, "size_gb": v.size,
            "attachments": [a.get("server_id") for a in (v.attachments or [])]}


def _image(i: Any) -> dict:
    return {"id": i.id, "name": i.name, "status": i.status,
            "disk_format": getattr(i, "disk_format", None),
            "visibility": getattr(i, "visibility", None)}


def _flavor(f: Any) -> dict:
    return {"id": f.id, "name": f.name, "vcpus": f.vcpus, "ram_mb": f.ram,
            "disk_gb": f.disk, "is_public": getattr(f, "is_public", None)}


# --- operations (take a connection) ---------------------------------------- #
def server_list(conn, all_projects: bool = True) -> list[dict]:
    return [_server(s) for s in conn.compute.servers(all_projects=all_projects)]


def server_show(conn, server_id: str) -> dict:
    s = conn.compute.find_server(server_id, ignore_missing=False)
    return _server(conn.compute.get_server(s.id))


def server_stop(conn, server_id: str) -> dict:
    s = conn.compute.find_server(server_id, ignore_missing=False)
    if s.status == "SHUTOFF":
        return {"id": s.id, "name": s.name, "status": s.status, "changed": False}
    conn.compute.stop_server(s)
    s = conn.compute.wait_for_server(s, status="SHUTOFF", wait=120)
    return {"id": s.id, "name": s.name, "status": s.status, "changed": True}


def server_start(conn, server_id: str) -> dict:
    s = conn.compute.find_server(server_id, ignore_missing=False)
    if s.status == "ACTIVE":
        return {"id": s.id, "name": s.name, "status": s.status, "changed": False}
    conn.compute.start_server(s)
    s = conn.compute.wait_for_server(s, status="ACTIVE", wait=120)
    return {"id": s.id, "name": s.name, "status": s.status, "changed": True}


def network_list(conn) -> list[dict]:
    return [_network(n) for n in conn.network.networks()]


def volume_list(conn, all_projects: bool = False) -> list[dict]:
    return [_volume(v) for v in conn.block_storage.volumes(all_projects=all_projects)]


def image_list(conn) -> list[dict]:
    return [_image(i) for i in conn.image.images()]


def flavor_list(conn) -> list[dict]:
    return [_flavor(f) for f in conn.compute.flavors()]


def service_status(conn) -> dict:
    """Health of OpenStack control/compute services and network agents."""
    comp = [{"binary": s.binary, "host": s.host, "state": getattr(s, "state", None),
             "status": getattr(s, "status", None),
             "zone": getattr(s, "availability_zone", None)}
            for s in conn.compute.services()]
    agents = []
    try:
        agents = [{"binary": a.binary, "host": a.host, "alive": getattr(a, "is_alive", None),
                   "type": getattr(a, "agent_type", None)} for a in conn.network.agents()]
    except Exception as e:  # OVN deployments may expose few/no agents
        agents = [{"note": f"network agents unavailable: {e}"}]
    down = [s for s in comp if (s["state"] or "").lower() == "down"]
    return {"compute_services": comp, "network_agents": agents,
            "summary": {"compute_total": len(comp), "compute_down": len(down),
                        "agents_total": len(agents)}}


def quota_show(conn, project_id) -> dict:
    """compute/block-storage/network quota(+usage where available) for a project,
    via the SDK. Each service guarded so one failing service doesn't kill the rest."""
    out = {"project_id": project_id, "limits": {}}
    try:
        out["limits"]["compute"] = conn.compute.get_quota_set(project_id, usage=True).to_dict()
    except Exception as e:
        out["limits"]["compute"] = {"error": str(e)}
    try:
        out["limits"]["block_storage"] = conn.block_storage.get_quota_set(project_id, usage=True).to_dict()
    except Exception as e:
        out["limits"]["block_storage"] = {"error": str(e)}
    try:
        out["limits"]["network"] = conn.network.get_quota(project_id, details=True).to_dict()
    except Exception as e:
        out["limits"]["network"] = {"error": str(e)}
    return out


def capacity_stats(conn) -> dict:
    agg: dict[str, dict] = {}
    hosts = 0
    for rp in conn.placement.resource_providers():
        hosts += 1
        invs = {i.resource_class: i for i in conn.placement.resource_provider_inventories(rp)}
        used = conn.placement.fetch_resource_provider_usages(rp).usages or {}
        for rc, inv in invs.items():
            cap = int((inv.total - inv.reserved) * inv.allocation_ratio)
            slot = agg.setdefault(rc, {"capacity": 0, "used": 0})
            slot["capacity"] += cap
            slot["used"] += int(used.get(rc, 0))
    for slot in agg.values():
        slot["free"] = slot["capacity"] - slot["used"]
    return {"hosts": hosts, "resources": agg}

import asyncio

from core.server import build_registry
from core.assembly import make_mcp
from core.registry import TIERS


def test_build_registry_has_os_only_compute_handtools():
    reg = build_registry()
    names = {t["name"]: t for t in reg.tools if t["domain"] == "compute"}
    assert {"server_stop", "server_start", "quota_show", "capacity_stats"} <= set(names)
    assert names["server_stop"]["tier"] == "write"
    assert names["server_start"]["tier"] == "write"
    assert names["quota_show"]["tier"] == "read"
    assert names["capacity_stats"]["tier"] == "read"


def test_core_has_no_agent_mode_tool():
    # agent_mode is external session state with no OpenStack equivalent —
    # it must not exist in this OpenStack-only edition.
    reg = build_registry()
    names = {t["name"] for t in reg.tools}
    assert "agent_mode" not in names


def test_compute_handtools_inject_ctx_not_expose_it():
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"compute"}, set(TIERS))
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    for name in ("server_stop", "quota_show"):
        assert name in tools
        schema = tools[name].inputSchema or {}
        props = (schema.get("properties") or {}).keys()
        assert "ctx" not in props, f"{name} exposes ctx as a client param: {list(props)}"
        assert "ctx" not in (schema.get("required") or [])

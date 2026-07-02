from core.server import build_registry, whoami
from core.assembly import make_mcp, env_set, instructions, scope_line, CONVENTIONS, DOMAIN_GIST
from core.registry import DOMAINS, TIERS


def test_build_registry_has_compute_tools_and_whoami():
    reg = build_registry()
    names = {t["name"] for t in reg.tools}
    assert "server_list" in names and "server_show" in names
    assert "whoami" in names


def test_make_mcp_builds_without_error():
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"compute"}, set(TIERS))
    assert m is not None


def test_whoami_no_creds_reports_unconfigured():
    out = whoami(ctx=None)          # no headers -> os_conn None
    assert out["edition"] == "openstack"
    assert out["openstack"]["configured"] is False


def test_factory_tools_inject_ctx_not_expose_it():
    # Regression: the factory inner fns must annotate `ctx: Context` so FastMCP
    # INJECTS the context rather than exposing `ctx` as a required client param.
    # Without the annotation every generated list/show tool 400s with
    # "ctx Field required" at call time (caught live, missed by direct-call unit tests).
    import asyncio
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"compute"}, set(TIERS))
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    for name in ("server_list", "server_show"):
        schema = tools[name].inputSchema or {}
        props = (schema.get("properties") or {}).keys()
        assert "ctx" not in props, f"{name} exposes ctx as a client param: {list(props)}"
        assert "ctx" not in (schema.get("required") or [])


def test_delete_update_tools_inject_ctx_not_expose_it():
    # Same regression as above, extended to the new write-tier factories: make_delete
    # is `async def _delete(ctx: Context, ...)` and make_update's __signature__ trick
    # both must keep ctx annotated so FastMCP injects it rather than exposing it.
    import asyncio
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"compute"}, set(TIERS))
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    assert "server_delete" in tools and "server_update" in tools
    for name in ("server_delete", "server_update"):
        schema = tools[name].inputSchema or {}
        props = (schema.get("properties") or {}).keys()
        assert "ctx" not in props, f"{name} exposes ctx as a client param: {list(props)}"
        assert "ctx" not in (schema.get("required") or [])
    update_props = (tools["server_update"].inputSchema or {}).get("properties") or {}
    assert "name" in update_props
    assert "description" in update_props


def test_domain_gist_has_seven_domains_incl_observability():
    assert "observability" in DOMAIN_GIST
    assert len(DOMAIN_GIST) == 7


def test_scope_line_differs_read_vs_write():
    read_line = scope_line({"read"})
    write_line = scope_line({"write"})
    assert "READ-ONLY" in read_line
    assert "READ-ONLY" not in write_line
    assert "SCOPE" in read_line and "SCOPE" in write_line


def test_instructions_contains_scope_and_neutral_conventions():
    text = instructions({"compute"}, {"read"})
    assert text.startswith("⚠️ SCOPE")
    assert "Conventions shared by every mount" in text
    assert CONVENTIONS in text
    assert scope_line({"read"}) in text


def test_make_mcp_instructions_property_matches_instructions_fn():
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"compute"}, {"read"})
    assert m.instructions == instructions({"compute"}, {"read"})


def test_whoami_with_conn_reports_current_project_and_roles(monkeypatch):
    import types
    import core.server as srv

    class _FakeAccess:
        project_name = "demo-project"
        role_names = ["member", "reader"]

    fake_conn = types.SimpleNamespace(
        current_project_id="proj-123",
        session=types.SimpleNamespace(
            auth=types.SimpleNamespace(get_access=lambda session: _FakeAccess())))
    monkeypatch.setattr(srv, "os_conn", lambda ctx: fake_conn)
    out = srv.whoami(ctx=None)
    assert out["openstack"]["configured"] is True
    assert out["openstack"]["current_project"] == {"id": "proj-123", "name": "demo-project"}
    assert out["openstack"]["roles"] == ["member", "reader"]


def test_whoami_current_project_lookup_failure_does_not_break_whoami(monkeypatch):
    import types
    import core.server as srv

    def _boom(session):
        raise RuntimeError("token expired")

    fake_conn = types.SimpleNamespace(
        current_project_id="proj-123",
        session=types.SimpleNamespace(auth=types.SimpleNamespace(get_access=_boom)))
    monkeypatch.setattr(srv, "os_conn", lambda ctx: fake_conn)
    out = srv.whoami(ctx=None)
    assert out["openstack"]["configured"] is True
    assert "current_project" not in out["openstack"]
    assert out["openstack"]["reachable"] is False


def test_service_status_returns_openstack_only_envelope(monkeypatch):
    import core.server as srv
    monkeypatch.setattr(srv, "os_conn", lambda ctx: object())
    monkeypatch.setattr(srv.os_backend, "service_status", lambda conn: {"nova-compute": "up"})
    out = srv.service_status(ctx=None)
    assert out == {"backend": "openstack", "backend_reason": "openstack_only",
                   "data": {"nova-compute": "up"}}

import asyncio

from core.server import build_registry
from core.assembly import make_mcp
from core.registry import TIERS
from core import observability as obs


def test_build_registry_has_kolla_only_observability_tools():
    reg = build_registry()
    obs_tools = {t["name"]: t for t in reg.tools if t["domain"] == "observability"}
    assert {"service_status", "log_targets", "log_tail", "log_trace"} <= set(obs_tools)
    for t in obs_tools.values():
        assert t["tier"] == "read"


def test_targets_returns_kolla_only(tmp_path, monkeypatch):
    kolla = tmp_path / "kolla" / "nova"
    kolla.mkdir(parents=True)
    (kolla / "nova-api.log").write_text("2026-07-02 00:00:00.000 INFO x\n")
    monkeypatch.setattr(obs, "KOLLA_LOG_DIR", str(tmp_path / "kolla"))
    out = obs.targets()
    assert out["kolla"] == ["nova"]
    assert set(out) == {"kolla", "node"}


def test_resolve_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "KOLLA_LOG_DIR", str(tmp_path))
    try:
        obs._resolve("unknown:batch")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "kolla" in str(e)


def test_tail_reads_kolla_log_in_time_window(tmp_path, monkeypatch):
    kolla = tmp_path / "nova"
    kolla.mkdir()
    (kolla / "nova-api.log").write_text(
        "2026-07-02 00:00:00.000 INFO [req-aaaaaaaa-1111-1111-1111-111111111111] hello\n")
    monkeypatch.setattr(obs, "KOLLA_LOG_DIR", str(tmp_path))
    out = obs.tail("kolla:nova", since="2026-07-01 00:00:00", until="2026-07-03 00:00:00")
    assert out["returned"] == 1
    assert out["records"][0]["req_global"] == "req-aaaaaaaa-1111-1111-1111-111111111111"


def test_factory_and_observability_tools_inject_ctx_not_expose_it():
    reg = build_registry()
    m = make_mcp(reg, "openstack", {"observability"}, set(TIERS))
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    for name in ("service_status", "log_targets", "log_tail", "log_trace"):
        assert name in tools
        schema = tools[name].inputSchema or {}
        props = (schema.get("properties") or {}).keys()
        assert "ctx" not in props, f"{name} exposes ctx as a client param: {list(props)}"
        assert "ctx" not in (schema.get("required") or [])

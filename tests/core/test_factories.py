import asyncio
import types

from core.factories import make_list, make_show, make_delete, make_update


class _FakeConn:  # os_list/os_show가 받는 conn
    pass


def _ctx_with_elicit(choice="delete", action="accept"):
    async def _elicit(message, schema):
        data = (types.SimpleNamespace(model_dump=lambda: {"choice": choice})
                if action == "accept" else None)
        return types.SimpleNamespace(action=action, data=data)
    return types.SimpleNamespace(elicit=_elicit)


def _spec_with(items=None, one=None):
    return {
        "name": "server",
        "fields": ["id", "name"],
        "os_list": lambda conn, allp: items,
        "os_show": lambda conn, i: one,
    }


def test_make_list_wraps_openstack_only_envelope(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    tool = make_list(_spec_with(items=[{"id": "1", "name": "a", "extra": "x"}]))
    out = tool(ctx=None)
    assert out["backend"] == "openstack"
    assert out["backend_reason"] == "openstack_only"
    assert out["data"] == [{"id": "1", "name": "a"}]   # summary projection


def test_make_list_raises_without_creds(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: None)
    tool = make_list(_spec_with(items=[]))
    try:
        tool(ctx=None)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_make_show_wraps_full(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    tool = make_show(_spec_with(one={"id": "1", "name": "a", "empty": None}))
    out = tool(ctx=None, resource_id="1")
    assert out == {"backend": "openstack", "backend_reason": "openstack_only",
                   "data": {"id": "1", "name": "a"}}


def test_make_delete_confirm_calls_os_delete_and_envelopes(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    calls = []
    spec = {"name": "server", "os_delete": lambda conn, i: calls.append(i)}
    tool = make_delete(spec)
    out = asyncio.run(tool(ctx=_ctx_with_elicit("delete"), resource_id="abc"))
    assert calls == ["abc"]
    assert out == {"backend": "openstack", "backend_reason": "openstack_only",
                   "data": {"deleted": "abc"}}


def test_make_delete_cancel_returns_cancelled_without_calling_os_delete(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    calls = []
    spec = {"name": "server", "os_delete": lambda conn, i: calls.append(i)}
    tool = make_delete(spec)
    out = asyncio.run(tool(ctx=_ctx_with_elicit("cancel"), resource_id="abc"))
    assert calls == []
    assert out == {"cancelled": True, "type": "server", "id": "abc"}


def test_make_delete_cancel_on_elicit_decline(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    spec = {"name": "server", "os_delete": lambda conn, i: None}
    tool = make_delete(spec)
    out = asyncio.run(tool(ctx=_ctx_with_elicit(action="decline"), resource_id="abc"))
    assert out == {"cancelled": True, "type": "server", "id": "abc"}


def test_make_delete_raises_without_creds(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: None)
    spec = {"name": "server", "os_delete": lambda conn, i: None}
    tool = make_delete(spec)
    try:
        asyncio.run(tool(ctx=_ctx_with_elicit("delete"), resource_id="abc"))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_make_delete_raises_without_os_delete(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    spec = {"name": "server"}
    tool = make_delete(spec)
    try:
        asyncio.run(tool(ctx=_ctx_with_elicit("delete"), resource_id="abc"))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_make_update_filters_body_and_envelopes(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    captured = {}

    def _os_update(conn, i, body):
        captured["body"] = body
        return {"id": i, **body}

    spec = {"name": "server", "update_fields": ["name", "description"], "os_update": _os_update}
    tool = make_update(spec)
    out = tool(ctx=None, resource_id="1", name="new-name", description=None)
    assert captured["body"] == {"name": "new-name"}
    assert out["backend"] == "openstack"
    assert out["backend_reason"] == "openstack_only"
    assert out["data"]["name"] == "new-name"


def test_make_update_raises_when_no_fields_given(monkeypatch):
    import core.factories as fac
    monkeypatch.setattr(fac, "os_conn", lambda ctx: _FakeConn())
    spec = {"name": "server", "update_fields": ["name"], "os_update": lambda c, i, b: b}
    tool = make_update(spec)
    try:
        tool(ctx=None, resource_id="1", name=None)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_make_update_signature_exposes_update_fields():
    import inspect
    spec = {"name": "server", "update_fields": ["name", "description"], "os_update": lambda c, i, b: b}
    tool = make_update(spec)
    params = inspect.signature(tool).parameters
    assert set(params) == {"ctx", "resource_id", "name", "description"}
    assert params["name"].kind == inspect.Parameter.KEYWORD_ONLY

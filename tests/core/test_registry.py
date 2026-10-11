from core.registry import Registry, register_resources
from core.specs import CORE_SPECS


def test_register_resources_builds_list_and_show_tools():
    reg = Registry()
    register_resources(reg, CORE_SPECS)
    names = {t["name"] for t in reg.tools}
    # 모든 리소스에 _list
    assert {"server_list", "flavor_list", "keypair_list",
            "hypervisor_list", "availability_zone_list"} <= names
    # os_show 있는 것만 _show (server, flavor)
    assert {"server_show", "flavor_show"} <= names
    assert "keypair_show" not in names          # os_show 없음
    # 도메인/티어 태깅
    server_list = next(t for t in reg.tools if t["name"] == "server_list")
    assert server_list["domain"] == "compute"
    assert server_list["tier"] == "read"


def test_kind_note_appended_to_description():
    reg = Registry()
    register_resources(reg, CORE_SPECS, kind_note=lambda spec: "ROUTED.")
    server_list = next(t for t in reg.tools if t["name"] == "server_list")
    assert server_list["description"].endswith("ROUTED.")


def test_delete_and_update_not_registered_without_factories():
    # Default (make_delete=None, make_update=None): nothing extra registered even
    # though CORE_SPECS now carries os_delete/os_update/update_fields for server.
    reg = Registry()
    register_resources(reg, CORE_SPECS)
    names = {t["name"] for t in reg.tools}
    assert "server_delete" not in names
    assert "server_update" not in names


def test_delete_and_update_registered_as_write_tier_when_factories_given():
    reg = Registry()
    register_resources(reg, CORE_SPECS,
                       make_delete=lambda spec: (lambda ctx, resource_id: None),
                       make_update=lambda spec: (lambda ctx, resource_id, **kw: None))
    names = {t["name"] for t in reg.tools}
    assert "server_delete" in names and "server_update" in names
    server_delete = next(t for t in reg.tools if t["name"] == "server_delete")
    server_update = next(t for t in reg.tools if t["name"] == "server_update")
    assert server_delete["tier"] == "write"
    assert server_update["tier"] == "write"
    # hypervisor has neither os_delete nor update_fields -> no tools
    assert "hypervisor_delete" not in names
    assert "hypervisor_update" not in names


def test_parent_list_replaces_normal_list():
    reg = Registry()
    spec = {"name": "server_action", "domain": "compute", "parent_path": "marker"}
    register_resources(reg, [spec],
                       make_parent_list=lambda spec: (lambda ctx, resource_id: None),
                       is_parent_scoped=lambda spec: bool(spec.get("parent_path")))
    names = [t["name"] for t in reg.tools]
    assert names.count("server_action_list") == 1
    assert "server_action_show" not in names


def test_parent_list_not_registered_without_is_parent_scoped_true():
    reg = Registry()
    spec = {"name": "flavor2", "domain": "compute"}
    register_resources(reg, [spec],
                       make_parent_list=lambda spec: (lambda ctx, resource_id: None))
    names = [t["name"] for t in reg.tools]
    assert "flavor2_list" in names   # normal list, not parent-scoped (default predicate is False)

import server

def test_imports_as_openstack_edition():
    # opit_backend 없음 → HAS_OPIT False, openstack 에디션으로 import 성공
    assert server.HAS_OPIT is False

def test_registry_nonempty():
    names = {t["name"] for t in server._REGISTRY}
    assert "server_list" in names
    assert "network_list" in names

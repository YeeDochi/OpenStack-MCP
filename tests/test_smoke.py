import server

def test_registry_nonempty():
    names = {t["name"] for t in server._REGISTRY}
    assert "server_list" in names
    assert "network_list" in names

def test_no_router_module():
    import importlib.util
    assert importlib.util.find_spec("router") is None


EXPECTED_PRESENT = {
    "whoami", "server_list", "server_show", "server_update", "server_delete",
    "server_start", "server_stop", "network_list", "subnet_list", "router_list",
    "volume_list", "image_list", "project_list", "user_list", "load_balancer_list",
    "flavor_list", "capacity_stats", "service_status", "log_tail", "log_targets",
    "gpu_server_list",
}
EXPECTED_ABSENT = {
    "agent_mode", "logout", "switch_project", "create_user", "create_user_form",
    "secret_list", "invoice_list", "stack_list", "cluster_list", "autoscale_list",
    "report_server_list", "pool_member_list", "user_role_list",
}

def test_expected_tools():
    names = {t["name"] for t in server._REGISTRY}
    assert EXPECTED_PRESENT <= names, EXPECTED_PRESENT - names
    assert not (EXPECTED_ABSENT & names), EXPECTED_ABSENT & names

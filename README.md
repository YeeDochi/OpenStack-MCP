# openstack-mcp

A Model Context Protocol (MCP) server for OpenStack — 123 tools across 7 domains, built on [openstacksdk](https://docs.openstack.org/openstacksdk/), with stateless per-caller header auth, a declarative RESOURCES registry, and Kolla log observability.

---

## Architecture

```
LLM client (Claude / any MCP host)
        │  MCP protocol (stdio or HTTP/SSE)
        ▼
┌─────────────────────────────────────────────────────┐
│                  server.py                          │
│  RESOURCES table → auto-generated list/show/        │
│  update/delete tools  +  hand-written specials      │
│                                                     │
│  _os_conn(ctx) ──► os_backend.py                   │
│  (per-caller creds)   openstacksdk Connection       │
│                       (Keystone app credential)     │
│                                ▼                    │
│                       OpenStack APIs                │
│                       Nova · Neutron · Cinder       │
│                       Glance · Keystone · Octavia   │
│                       Placement                     │
│                                                     │
│  ops_backend.py ──► Kolla log files (read-only)    │
│  (observability)      /var/log/kolla/*              │
└─────────────────────────────────────────────────────┘

Per-domain HTTP mounts (stateful sessions for elicitation):
  /compute/mcp   /network/mcp   /lbaas/mcp
  /storage/mcp   /image/mcp     /identity/mcp
  /observability/mcp
```

Each domain is an independent FastMCP instance. A shared process exposes all mounts; `OSMCP_DOMAINS` and `OSMCP_TIERS` narrow which tools are active.

---

## Features

- **Declarative registry** — `RESOURCES` table + `_make_list/_make_show/_make_update/_make_delete` generators; adding a new resource is one dict entry.
- **Stateless per-caller auth** — credentials are read from request headers on every call (HTTP) or from env vars (stdio). The server stores nothing; multiple callers with different credentials share one process safely.
- **Structured error envelope** — all tool errors surface as `Error executing tool <name>: {"error":{"type","message","http_status?}}`. Parse from the first `{`.
- **Delete confirmation** — `*_delete` tools use MCP elicitation to require an explicit human `"delete"` choice before executing. Irreversible operations cannot be triggered by an LLM alone.
- **Key-columns / detail** — list tools return a compact key-column view by default; pass `detail=True` for all fields. `limit=N` caps row count. `all_projects=True` for the admin view where supported.
- **Multimount** — 7 per-domain FastMCP instances served at `/<domain>/mcp`, each carrying a routing map in its `initialize` instructions so clients pick the right mount on the first try.
- **Kolla log observability** — `log_targets`, `log_tail`, `log_trace` read Kolla service log files directly from the host filesystem (mounted read-only), with time-window filtering, regex grep, and request-ID cross-service tracing.

---

## Quickstart

### Prerequisites

- Python 3.10+
- An OpenStack cloud with Keystone **application credentials** (project or domain scope depending on the operations you want)
- For Kolla log tools: `/var/log/kolla` accessible on the server host (or mounted into the container)

### Install

```bash
git clone https://github.com/your-user/openstack-mcp.git
cd openstack-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### stdio mode (Claude Desktop / `claude mcp add`)

Set env vars, then run:

```bash
export OS_AUTH_URL=https://keystone.example.com:5000/v3
export OS_APPLICATION_CREDENTIAL_ID=<your-app-cred-id>
export OS_APPLICATION_CREDENTIAL_SECRET=<your-app-cred-secret>

python src/server.py --transport stdio
```

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "openstack": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/openstack-mcp/src/server.py", "--transport", "stdio"],
      "env": {
        "OS_AUTH_URL": "https://keystone.example.com:5000/v3",
        "OS_APPLICATION_CREDENTIAL_ID": "...",
        "OS_APPLICATION_CREDENTIAL_SECRET": "..."
      }
    }
  }
}
```

Or via the CLI:

```bash
claude mcp add openstack -- /path/to/.venv/bin/python /path/to/openstack-mcp/src/server.py
```

### HTTP mode (multi-user / container)

```bash
# Copy and edit the example config
cp config.env.example config.env
# Set OS_AUTH_URL, MCP_PORT, KOLLA_LOG_DIR, etc.

python src/server.py --transport http --host 0.0.0.0 --port 8001
```

Per-domain endpoints:

```
http://localhost:8001/compute/mcp
http://localhost:8001/network/mcp
http://localhost:8001/lbaas/mcp
http://localhost:8001/storage/mcp
http://localhost:8001/image/mcp
http://localhost:8001/identity/mcp
http://localhost:8001/observability/mcp
```

Pass credentials in request headers on every call:

```
X-OS-App-Cred-Id:     <application-credential-id>
X-OS-App-Cred-Secret: <application-credential-secret>
X-OS-Auth-Url:        https://keystone.example.com:5000/v3   # optional override
```

#### Container (Containerfile provided)

```bash
podman build -t openstack-mcp .
podman run --rm -p 8001:8001 \
  --env-file config.env \
  -v /var/log/kolla:/var/log/kolla:ro \
  openstack-mcp
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8001` | HTTP listen port |
| `OS_AUTH_URL` | `http://127.0.0.1:5000/v3` | Keystone endpoint (server default; overridable per-caller) |
| `OSMCP_DOMAINS` | all | Comma-separated subset: `compute,network,lbaas,storage,image,identity,observability` |
| `OSMCP_TIERS` | all | Comma-separated subset: `read,write,maintain` |
| `KOLLA_LOG_DIR` | `/var/log/kolla` | Root of Kolla service log directories |
| `MCP_NODE_NAME` | hostname | Label identifying which node's logs are being served |
| `MCP_ALLOWED_HOST_NAMES` | `localhost,127.0.0.1` | Comma-separated hostnames for HTTP host-header allowlist |

---

## Tool Reference

### Core (all domains)

| Tool | Description |
|---|---|
| `whoami` | Show credentials presence and current project/roles |

### Compute

| Tool | Description |
|---|---|
| `server_list` | List instances (key columns: id, name, status) |
| `server_show` | Show one instance by id |
| `server_update` | Update instance name/description |
| `server_delete` | Delete instance (requires human confirmation) |
| `server_start` | Power on a SHUTOFF instance |
| `server_stop` | Power off an ACTIVE instance |
| `flavor_list` | List compute flavors |
| `flavor_show` | Show one flavor |
| `flavor_delete` | Delete a flavor |
| `keypair_list` | List keypairs |
| `keypair_delete` | Delete a keypair |
| `hypervisor_list` | List compute hypervisors |
| `availability_zone_list` | List availability zones |
| `aggregate_list` | List host aggregates (admin) |
| `aggregate_show` | Show one host aggregate |
| `aggregate_update` | Update aggregate name |
| `aggregate_delete` | Delete host aggregate |
| `server_group_list` | List server groups |
| `server_group_show` | Show one server group |
| `server_group_delete` | Delete server group |
| `quota_show` | Compute/network/storage quota + usage for a project |
| `capacity_stats` | Aggregate vCPU/RAM/disk capacity vs usage (Placement) |

### Network

| Tool | Description |
|---|---|
| `network_list` | List networks |
| `network_show` | Show one network |
| `network_update` | Update network name/description |
| `network_delete` | Delete network |
| `subnet_list` | List subnets |
| `subnet_show` | Show one subnet |
| `subnet_update` | Update subnet |
| `subnet_delete` | Delete subnet |
| `router_list` | List routers |
| `router_show` | Show one router |
| `router_update` | Update router |
| `router_delete` | Delete router |
| `port_list` | List ports |
| `port_show` | Show one port |
| `port_update` | Update port |
| `port_delete` | Delete port |
| `security_group_list` | List security groups |
| `security_group_show` | Show one security group |
| `security_group_update` | Update security group |
| `security_group_delete` | Delete security group |
| `security_group_rule_list` | List security group rules |
| `security_group_rule_delete` | Delete security group rule |
| `floating_ip_list` | List floating IPs |
| `floating_ip_show` | Show one floating IP |
| `floating_ip_update` | Update floating IP |
| `floating_ip_delete` | Release floating IP |
| `agent_list` | List Neutron agents (admin) |
| `agent_show` | Show one agent |
| `rbac_policy_list` | List RBAC policies |
| `rbac_policy_show` | Show one RBAC policy |
| `network_ip_availability_list` | IP availability per network (admin) |
| `network_ip_availability_show` | Show IP availability for one network |

### LBaaS (Octavia)

| Tool | Description |
|---|---|
| `load_balancer_list` | List load balancers |
| `load_balancer_show` | Show one load balancer |
| `load_balancer_update` | Update load balancer |
| `load_balancer_delete` | Delete load balancer |
| `listener_list` | List listeners |
| `listener_show` | Show one listener |
| `listener_update` | Update listener |
| `listener_delete` | Delete listener |
| `pool_list` | List pools |
| `pool_show` | Show one pool |
| `pool_update` | Update pool |
| `pool_delete` | Delete pool |
| `health_monitor_list` | List health monitors |
| `health_monitor_show` | Show one health monitor |
| `health_monitor_update` | Update health monitor |
| `health_monitor_delete` | Delete health monitor |
| `l7_policy_list` | List L7 policies |
| `l7_policy_show` | Show one L7 policy |
| `l7_policy_delete` | Delete L7 policy |
| `lb_flavor_list` | List LBaaS flavors |
| `lb_flavor_show` | Show one LBaaS flavor |

### Storage (Cinder)

| Tool | Description |
|---|---|
| `volume_list` | List block volumes |
| `volume_show` | Show one volume |
| `volume_update` | Update volume name/description |
| `volume_delete` | Delete volume |
| `volume_snapshot_list` | List snapshots |
| `volume_snapshot_show` | Show one snapshot |
| `volume_snapshot_delete` | Delete snapshot |
| `volume_type_list` | List volume types |
| `volume_backup_list` | List backups |
| `volume_backup_show` | Show one backup |
| `volume_backup_delete` | Delete backup |
| `volume_group_list` | List volume groups |
| `volume_group_show` | Show one group |
| `volume_group_type_list` | List group types |
| `volume_group_type_show` | Show one group type |
| `volume_group_snapshot_list` | List group snapshots |
| `volume_group_snapshot_show` | Show one group snapshot |
| `volume_service_list` | List Cinder backend services (admin) |

### Image (Glance)

| Tool | Description |
|---|---|
| `image_list` | List images |
| `image_show` | Show one image |
| `image_delete` | Delete image |
| `metadef_namespace_list` | List metadata definition namespaces |
| `metadef_namespace_show` | Show one namespace |

### Identity (Keystone)

| Tool | Description |
|---|---|
| `project_list` | List projects |
| `project_show` | Show one project |
| `project_delete` | Delete project |
| `domain_list` | List domains |
| `domain_show` | Show one domain |
| `domain_delete` | Delete domain |
| `user_list` | List users |
| `user_show` | Show one user |
| `user_update` | Update user name/email |
| `user_delete` | Delete user |
| `role_list` | List roles |
| `role_delete` | Delete role |
| `role_assignment_list` | List role assignments |
| `application_credential_list` | List app credentials (current user) |
| `region_list` | List regions |
| `region_show` | Show one region |
| `service_list` | List catalog services (admin) |
| `service_show` | Show one catalog service |
| `endpoint_list` | List catalog endpoints (admin) |
| `endpoint_show` | Show one endpoint |

### Observability

| Tool | Description |
|---|---|
| `log_targets` | List available Kolla log targets (per-service dirs) |
| `log_tail` | Tail one target log with time-window + grep filtering |
| `log_trace` | Cross-service trace by OpenStack request ID (`req-...`) |
| `service_status` | Nova compute services + Neutron agents health (up/down) |

---

## Extending: add a new resource

Create tools is intentionally not implemented — it is the primary extension point. To add a create tool or a new resource type:

1. Add a function in `src/server.py` or `src/os_backend.py` using openstacksdk.
2. Register it with `add(fn, name="...", domain="...", tier="write")`.
3. For a full CRUD resource, add one dict to `RESOURCES` and a `RESOURCE_DOMAIN` mapping entry; `_make_list/_make_show/_make_update/_make_delete` generate the tools automatically.

Any OpenStack service supported by openstacksdk can be wired in this way with a handful of lines.

---

## Running tests

```bash
pytest -q
```

The smoke suite verifies: no forbidden tokens in `src/*.py` or root `*.md`, registry is non-empty and contains expected tools, no legacy router module present.

---

## License

MIT — see [LICENSE](LICENSE).

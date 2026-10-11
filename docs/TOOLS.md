# Tool Reference

**English** | [한국어](TOOLS.ko.md) · [← README](../README.md)

123 tools across 7 domains. `*_delete` tools require an explicit human confirmation via MCP elicitation. See [USAGE.md](USAGE.md) for how to run the server.

---

## Core (all domains)

| Tool | Description |
|---|---|
| `whoami` | Show credentials presence and current project/roles |

## Compute (Nova)

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

## Network (Neutron)

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

## LBaaS (Octavia)

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

## Storage (Cinder)

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

## Image (Glance)

| Tool | Description |
|---|---|
| `image_list` | List images |
| `image_show` | Show one image |
| `image_delete` | Delete image |
| `metadef_namespace_list` | List metadata definition namespaces |
| `metadef_namespace_show` | Show one namespace |

## Identity (Keystone)

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

## Observability

| Tool | Description |
|---|---|
| `log_targets` | List available Kolla log targets (per-service dirs) |
| `log_tail` | Tail one target log with time-window + grep filtering |
| `log_trace` | Cross-service trace by OpenStack request ID (`req-...`) |
| `service_status` | Nova compute services + Neutron agents health (up/down) |

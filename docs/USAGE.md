# Usage

**English** | [한국어](USAGE.ko.md) · [← README](../README.md)

How to install and run OpenStack-MCP in stdio and HTTP modes. For the full tool list see [TOOLS.md](TOOLS.md).

---

## Prerequisites

- Python 3.10+
- An OpenStack cloud with Keystone **application credentials** (project or domain scope depending on the operations you want)
- For Kolla log tools: `/var/log/kolla` accessible on the server host (or mounted into the container)

## Install

```bash
git clone https://github.com/YeeDochi/OpenStack-MCP.git
cd OpenStack-MCP
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## stdio mode (Claude Desktop / `claude mcp add`)

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
      "args": ["/path/to/OpenStack-MCP/src/server.py", "--transport", "stdio"],
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
claude mcp add --transport stdio openstack -- /path/to/.venv/bin/python /path/to/OpenStack-MCP/src/server.py
```

---

## HTTP mode (multi-user / container)

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

### Container (Containerfile provided)

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
| `MCP_ALLOWED_HOST_NAMES` | `localhost,127.0.0.1` | Comma-separated hostnames; each is suffixed with `:<MCP_PORT>` to form the Host-header allowlist. Set `MCP_ALLOWED_HOSTS` to override the full list directly. |

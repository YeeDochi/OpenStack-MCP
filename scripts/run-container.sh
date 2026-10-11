#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ENGINE="${ENGINE:-podman}"
"$ENGINE" build -t openstack-mcp:latest .
"$ENGINE" run -d --name openstack-mcp --restart=always --network host \
  --env-file config.env \
  -v /var/log/kolla:/var/log/kolla:ro \
  openstack-mcp:latest \
  python -m core.server --transport http --host 0.0.0.0

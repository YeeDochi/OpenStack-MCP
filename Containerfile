# Image for the adaptive OpenStack/OpenStackit MCP.
# Built from a per-edition staged context (see build-images.sh) — the context
# contains only the files for that edition, so the 'openstack' image ships NO
# OpenStackit source.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Defaults — override at run time with -e. No credentials are baked in;
# callers supply them per request via headers.
# MCP_PORT is the single source of truth: it sets the listen port AND derives the
# Host-header allowlist (see server.py). Change the port with `-e MCP_PORT=8002`
# (and publish that port). The CMD omits --port so it inherits MCP_PORT.
ENV OPENSTACKIT_BASE_URL=https://192.168.140.14:5529 \
    OPENSTACKIT_VERIFY_TLS=false \
    OS_AUTH_URL=http://192.168.140.14:5000/v3 \
    MCP_PORT=8001

EXPOSE 8001
CMD ["python", "src/server.py", "--transport", "http", "--host", "0.0.0.0"]

# OpenStack MCP container image.
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
ENV OS_AUTH_URL=http://127.0.0.1:5000/v3 \
    MCP_PORT=8001

EXPOSE 8001
CMD ["python", "src/server.py", "--transport", "http", "--host", "0.0.0.0"]

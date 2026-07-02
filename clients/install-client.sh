#!/usr/bin/env bash
# Register the OpenStack MCP in Claude Code on THIS machine.
#
# This is an HTTP MCP — the client needs only Claude Code + network access to the
# server. No Python or dependencies are installed here; this just registers the
# endpoint and your per-user credentials.
#
# The server is a multimount MCP: one container, one port, a separate MCP endpoint
# per domain at <base>/<domain>/mcp (plus a combined <base>/mcp). This registers one
# Claude Code entry per domain (openstack-compute, openstack-network, ...). Pick a
# subset with DOMAINS, or register the single combined endpoint with DOMAINS=all.
#
# Usage:
#   ./install-client.sh                 # prompts for credentials, registers all domains
#   BASE_URL=http://host:8001 DOMAINS="compute network" OS_APP_CRED_ID=... \
#     OS_APP_CRED_SECRET=... ./install-client.sh
set -euo pipefail

PREFIX="${MCP_NAME:-openstack}"   # entries are named <PREFIX>-<domain>
SCOPE="${SCOPE:-user}"            # local | user | project ; user = available everywhere
DOMAINS="${DOMAINS:-compute network storage lbaas image identity observability}"

command -v claude >/dev/null || { echo "ERROR: 'claude' CLI not found. Install Claude Code first."; exit 1; }

prompt() { local v d="${2:-}"; read -rp "$1${d:+ [$d]}: " v; echo "${v:-$d}"; }
prompt_secret() { local v; read -rsp "$1: " v; echo >&2; echo "$v"; }

# Server base (scheme://host:port, NO path), and the OpenStack cloud it talks to for YOU.
BASE_URL="${BASE_URL:-$(prompt 'MCP server base URL (no path)' 'http://192.168.140.14:8001')}"
BASE_URL="${BASE_URL%/}"
OS_AUTH_URL_IN="${OS_AUTH_URL:-$(prompt 'OpenStack (Keystone) auth URL' 'http://192.168.140.14:5000/v3')}"

# Your credentials for that cloud.
OS_APP_CRED_ID="${OS_APP_CRED_ID:-$(prompt 'OpenStack app-credential id')}"
OS_APP_CRED_SECRET="${OS_APP_CRED_SECRET:-$(prompt_secret 'OpenStack app-credential secret')}"

add_entry() {  # $1 = entry name, $2 = url
  claude mcp remove --scope "$SCOPE" "$1" >/dev/null 2>&1 || true
  claude mcp add --transport http --scope "$SCOPE" "$1" "$2" \
    -H "X-OS-Auth-Url: $OS_AUTH_URL_IN" \
    -H "X-OS-App-Cred-Id: $OS_APP_CRED_ID" \
    -H "X-OS-App-Cred-Secret: $OS_APP_CRED_SECRET"
  echo "Registered  $1  -> $2"
}

echo
REG_NAMES=""
for d in $DOMAINS; do
  if [ "$d" = "all" ]; then
    name="$PREFIX"; url="$BASE_URL/mcp"
  else
    name="$PREFIX-$d"; url="$BASE_URL/$d/mcp"
  fi
  add_entry "$name" "$url"
  REG_NAMES="$REG_NAMES $name"
done
echo
echo "Done (scope: $SCOPE). Verify with:  claude mcp list"
echo
echo "NOTE: Claude Code may prompt for permission on first use of each tool."
echo "      If you want to pre-approve these servers, add to your settings.json"
echo "      under permissions.allow, one entry per registered server:"
for name in $REG_NAMES; do echo "        mcp__${name}__*"; done

# Install bundled client-side skills, if this package includes any (each skill is
# a subfolder under ../skills/; the README-only scaffold has no subfolders).
SKILLS_SRC="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/skills"
if [ -d "$SKILLS_SRC" ] && [ -n "$(find "$SKILLS_SRC" -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null)" ]; then
  DEST="${HOME}/.claude/skills"
  mkdir -p "$DEST"
  for d in "$SKILLS_SRC"/*/; do
    [ -d "$d" ] || continue
    cp -r "$d" "$DEST/"
    echo "Installed skill: $(basename "$d") -> $DEST/$(basename "$d")"
  done
fi

# Auto-save this connection as an /openstack-profile profile (reuses the bundled
# openstack-profile.sh), so the user starts with one profile without re-typing.
PROFILE_SH="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/skills/openstack-profile/openstack-profile.sh"
if [ -f "$PROFILE_SH" ]; then
  PROFILE_NAME="${PROFILE:-$(prompt 'Save this connection as profile (blank to skip)' 'default')}"
  if [ -n "$PROFILE_NAME" ]; then
    if OPENSTACK_PROFILE_NONINTERACTIVE=1 \
       BASE_URL="$BASE_URL" OS_APP_CRED_ID="$OS_APP_CRED_ID" OS_APP_CRED_SECRET="$OS_APP_CRED_SECRET" \
       OS_AUTH_URL="$OS_AUTH_URL_IN" \
       bash "$PROFILE_SH" add "$PROFILE_NAME"; then
      echo "Saved profile '$PROFILE_NAME' — switch later with /openstack-profile."
    else
      echo "(profile auto-save skipped)"
    fi
  fi
fi

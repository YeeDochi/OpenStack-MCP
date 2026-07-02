#!/usr/bin/env bash
# /openstack-profile backend (Unix). Manage client connection profiles + switch the
# active MCP registration. Profiles live in $OPENSTACK_PROFILE_DIR (default
# ~/.config/openstack-mcp/profiles). `claude` is called via $CLAUDE_BIN
# (default 'claude') so it can be stubbed in tests.
set -euo pipefail

PROFILE_DIR="${OPENSTACK_PROFILE_DIR:-$HOME/.config/openstack-mcp/profiles}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
SCOPE="${SCOPE:-user}"
PREFIX="${MCP_NAME:-openstack}"
DEFAULT_DOMAINS="compute network storage lbaas image identity observability"

usage() { echo "usage: openstack-profile.sh [list | add <name> | switch <name> | remove <name>]"; }

# Interactive prompt helpers (used by `add` when stdin is a TTY).
prompt_req() {  # $1=varname $2=label $3=hidden(1/0) — re-asks until non-empty
  local _v=""
  while [ -z "$_v" ]; do
    if [ "${3:-0}" = 1 ]; then read -rsp "$2: " _v || true; echo; else read -rp "$2: " _v || true; fi
    [ -z "$_v" ] && echo "  (required)" >&2
  done
  printf -v "$1" '%s' "$_v"
}
prompt_opt() {  # $1=varname $2=label $3=hidden(1/0) — Enter to skip
  local _v=""
  if [ "${3:-0}" = 1 ]; then read -rsp "$2 [Enter=skip]: " _v || true; echo; else read -rp "$2 [Enter=skip]: " _v || true; fi
  printf -v "$1" '%s' "$_v"
}

cmd="${1:-list}"; name="${2:-}"

if [ -n "$name" ] && ! printf '%s' "$name" | grep -qE '^[A-Za-z0-9_-]+$'; then
  echo "invalid profile name: '$name' (use letters/digits/_/- only)"; exit 1
fi

case "$cmd" in
  list)
    mkdir -p "$PROFILE_DIR"
    active=""; [ -f "$PROFILE_DIR/.active" ] && active="$(cat "$PROFILE_DIR/.active" 2>/dev/null)"
    echo "profiles ($PROFILE_DIR):"
    found=0
    for f in "$PROFILE_DIR"/*.env; do
      [ -e "$f" ] || break
      n="$(basename "$f" .env)"; found=1
      if [ "$n" = "$active" ]; then echo "  * $n  (active)"; else echo "  - $n"; fi
    done
    [ "$found" = 1 ] || echo "  (none)"
    ;;

  add)
    [ -n "$name" ] || { usage; exit 1; }
    # FROM=<existing>: seed defaults from that profile WITHOUT clobbering caller-provided env.
    if [ -n "${FROM:-}" ]; then
      ff="$PROFILE_DIR/$FROM.env"
      [ -f "$ff" ] || { echo "no such source profile: $FROM"; exit 1; }
      while IFS='=' read -r k v; do
        [ -z "$k" ] && continue
        case "$k" in \#*) continue ;; esac
        v="${v#\'}"; v="${v%\'}"      # strip the quotes add wrote around DOMAINS
        [ -z "${!k:-}" ] && export "$k=$v"
      done < "$ff"
    fi
    # Hybrid input: prompt for any missing field when interactive. A value already
    # in the environment (incl. inherited via FROM) is kept and never prompted, so
    # non-interactive callers (automation/tests passing env) fall through to the
    # checks below instead of hanging on a prompt.
    # DOMAINS is intentionally NOT prompted (advanced; defaults to all). Set it via
    # env only — `switch` still honors a DOMAINS line if a profile has one.
    if [ -t 0 ] && [ -z "${OPENSTACK_PROFILE_NONINTERACTIVE:-}" ]; then
      [ -n "${BASE_URL:-}" ]           || prompt_req BASE_URL "BASE_URL (e.g. http://192.168.140.14:8001 — scheme required, no path)" 0
      [ -n "${OS_AUTH_URL:-}" ]        || prompt_req OS_AUTH_URL "OS_AUTH_URL (e.g. http://192.168.140.14:5000/v3)" 0
      [ -n "${OS_APP_CRED_ID:-}" ]     || prompt_req OS_APP_CRED_ID "OS_APP_CRED_ID" 0
      [ -n "${OS_APP_CRED_SECRET:-}" ] || prompt_req OS_APP_CRED_SECRET "OS_APP_CRED_SECRET" 1
    fi
    : "${BASE_URL:?BASE_URL required}"
    : "${OS_AUTH_URL:?OS_AUTH_URL required}"
    : "${OS_APP_CRED_ID:?OS_APP_CRED_ID required}"
    : "${OS_APP_CRED_SECRET:?OS_APP_CRED_SECRET required}"
    # A schemeless BASE_URL produces a broken MCP url (host:port/compute/mcp) at switch
    # time. Assume http:// when no scheme was given, and strip any trailing slash.
    case "$BASE_URL" in
      http://*|https://*) ;;
      *) echo "note: BASE_URL has no scheme — assuming http://$BASE_URL" >&2; BASE_URL="http://$BASE_URL" ;;
    esac
    BASE_URL="${BASE_URL%/}"
    mkdir -p "$PROFILE_DIR"
    f="$PROFILE_DIR/$name.env"
    {
      echo "BASE_URL=$BASE_URL"
      echo "OS_AUTH_URL=$OS_AUTH_URL"
      echo "OS_APP_CRED_ID=$OS_APP_CRED_ID"
      echo "OS_APP_CRED_SECRET=$OS_APP_CRED_SECRET"
      [ -n "${DOMAINS:-}" ] && echo "DOMAINS='$DOMAINS'"
    } > "$f"
    chmod 600 "$f"
    echo "wrote profile '$name' -> $f"
    ;;

  switch)
    [ -n "$name" ] || { usage; exit 1; }
    f="$PROFILE_DIR/$name.env"
    [ -f "$f" ] || { echo "no such profile: $name (see: openstack-profile.sh list)"; exit 1; }
    # Parse line-by-line — never source (a secret with shell metacharacters must
    # not be executed). Whitelist keys; strip the quotes add wrote around DOMAINS only.
    BASE_URL= OS_AUTH_URL= OS_APP_CRED_ID= OS_APP_CRED_SECRET= DOMAINS=
    while IFS='=' read -r k v; do
      case "$k" in
        \#*|'') continue ;;
        DOMAINS) v="${v#\'}"; v="${v%\'}" ;;
      esac
      case "$k" in
        BASE_URL|OS_AUTH_URL|OS_APP_CRED_ID|OS_APP_CRED_SECRET|DOMAINS)
          printf -v "$k" '%s' "$v" ;;
      esac
    done < "$f"
    domains="${DOMAINS:-$DEFAULT_DOMAINS}"
    add_entry() {  # $1 name  $2 url
      "$CLAUDE_BIN" mcp remove --scope "$SCOPE" "$1" >/dev/null 2>&1 || true
      "$CLAUDE_BIN" mcp add --transport http --scope "$SCOPE" "$1" "$2" \
        -H "X-OS-Auth-Url: $OS_AUTH_URL" \
        -H "X-OS-App-Cred-Id: $OS_APP_CRED_ID" \
        -H "X-OS-App-Cred-Secret: $OS_APP_CRED_SECRET"
      echo "registered $1 -> $2"
    }
    for d in $domains; do add_entry "$PREFIX-$d" "$BASE_URL/$d/mcp"; done
    printf '%s' "$name" > "$PROFILE_DIR/.active"
    echo "switched to '$name' ($BASE_URL)."
    echo "apply with:  claude --continue   (resume latest conversation)   or   claude --resume   (pick a session)"
    echo "  -> keeps your conversation; reloads the new MCP registration. (a plain restart also works but loses the chat.)"
    ;;

  remove)
    [ -n "$name" ] || { usage; exit 1; }
    f="$PROFILE_DIR/$name.env"
    if [ -f "$f" ]; then
      rm -f "$f"
      [ -f "$PROFILE_DIR/.active" ] && [ "$(cat "$PROFILE_DIR/.active" 2>/dev/null)" = "$name" ] && rm -f "$PROFILE_DIR/.active"
      echo "removed profile '$name'"
    else
      echo "no such profile: $name"
    fi
    ;;

  *) usage; exit 1 ;;
esac

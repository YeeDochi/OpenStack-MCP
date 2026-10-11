"""Per-caller OpenStack credentials/endpoints from request headers (env fallback)."""
from __future__ import annotations

import os

from core import os_backend

OS_AUTH_URL = os.environ.get("OS_AUTH_URL", "http://127.0.0.1:5000/v3")
_osp = os_backend.OpenStackProvider(OS_AUTH_URL)


def headers(ctx):
    try:
        return ctx.request_context.request.headers
    except Exception:
        return {}


def os_auth_url(ctx):
    return headers(ctx).get("x-os-auth-url") or OS_AUTH_URL


def os_creds(ctx):
    h = headers(ctx)
    cid = h.get("x-os-app-cred-id") or os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
    sec = h.get("x-os-app-cred-secret") or os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    return (cid, sec) if cid and sec else None


def os_conn(ctx):
    creds = os_creds(ctx)
    return _osp.conn(*creds, auth_url=os_auth_url(ctx)) if creds else None

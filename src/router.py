"""Adaptive routing between the OpenStackit and OpenStack backends.

Policy (agreed with the user):
  - Prefer OpenStackit when available (it carries validation/governance).
  - Fall back to OpenStack ONLY on infrastructure failure (5xx / connection) —
    these mean OpenStackit is broken, not deliberately refusing.
  - Do NOT fall back on a deliberate 4xx rejection (validation, LB-in-use, bad
    request): that's the safeguard working; surface it.
  - 'never' policy = safeguard-critical op; never fall back even on 5xx.
  - On fallback, the OpenStackit error is included in the result for transparency.

Every result carries `backend` so the caller knows what served the request.
"""
from __future__ import annotations

from errors import OpenStackitError


def route(*, openstackit=None, openstack=None, policy="auto",
          openstackit_available=True, prefer="auto"):
    """Execute an operation across backends per policy.

    openstackit / openstack: zero-arg callables returning the result, or None if
    that backend has no implementation for this operation.
    policy: 'auto' | 'never' | 'openstack_only'
    prefer: caller override — 'auto' (use policy), 'openstack' (force raw
            OpenStack, bypassing OpenStackit), or 'openstackit' (force
            OpenStackit, no fallback). Ignored when the forced backend has no
            implementation for this op (e.g. forcing OpenStack on a never op).
    """
    # Every result carries `backend_reason` so callers can tell WHY a backend was
    # used: primary (first choice ran, no fallback) | fallback (OpenStackit failed →
    # OpenStack) | forced (caller's backend= override) | openstack_only (op has only
    # an OpenStack impl). Useful for diagnosing whether a fallback silently happened.
    # Caller override takes precedence over the default policy.
    if prefer == "openstack" and openstack is not None:
        return {"backend": "openstack", "backend_reason": "forced", "data": openstack()}
    if prefer == "openstackit":
        if openstackit is None or not openstackit_available:
            raise RuntimeError(
                "backend 'openstackit' was forced but it is unavailable or has "
                "no implementation for this operation")
        return {"backend": "openstackit", "backend_reason": "forced", "data": openstackit()}

    if policy == "never":
        # OpenStackit-only value-add: must run on OpenStackit, never fall back.
        if openstackit is None or not openstackit_available:
            raise RuntimeError(
                "this operation requires OpenStackit (credentials missing or "
                "OpenStackit not available); it has no OpenStack equivalent")
        return {"backend": "openstackit", "backend_reason": "primary", "data": openstackit()}

    if policy == "openstack_only" or openstackit is None:
        if openstack is None:
            raise RuntimeError("no OpenStack implementation for this operation")
        return {"backend": "openstack", "backend_reason": "openstack_only", "data": openstack()}

    if not openstackit_available:
        if openstack is None:
            raise RuntimeError(
                "OpenStackit is not available and there is no OpenStack fallback")
        return {"backend": "openstack", "backend_reason": "fallback", "data": openstack(),
                "note": "OpenStackit not available; used OpenStack"}

    try:
        return {"backend": "openstackit", "backend_reason": "primary", "data": openstackit()}
    except OpenStackitError as e:
        # Fall back to OpenStack on infra failure (5xx / connection) OR auth failure
        # (401 = session expired and re-login also failed — the client already tried
        # to re-login once). A deliberate rejection (4xx incl. 403 permission) or a
        # 'never'/safeguard op surfaces instead. Stay transparent about why.
        if policy == "never" or openstack is None or not (e.is_infra_failure or e.is_auth_failure):
            raise
        return {"backend": "openstack", "backend_reason": "fallback", "data": openstack(),
                "fell_back": True, "openstackit_error": str(e),
                "openstackit_http_status": e.http_status}

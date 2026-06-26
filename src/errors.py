"""Shared error types (always shipped, both editions).

OpenStackitError lives here — not in openstackit_client — so router.py can
classify fallback eligibility without importing the OpenStackit-only client
(which is absent in the 'openstack' edition).
"""
from __future__ import annotations


class OpenStackitError(Exception):
    """Login or API failure.

    `code` is OpenStackit's business error code when known. `http_status` is the
    HTTP status (None for connection-level failures). `body` is the raw response
    snippet. The router uses http_status to decide fallback: 5xx / None (infra
    failure) is fallback-eligible; 4xx (deliberate rejection) is not.
    """

    def __init__(self, message: str, code: str | None = None,
                 http_status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.body = body

    @property
    def is_infra_failure(self) -> bool:
        """True if this looks like OpenStackit being broken/unreachable, not a
        deliberate business rejection — i.e. safe to fall back to OpenStack."""
        return self.http_status is None or self.http_status >= 500

    @property
    def is_auth_failure(self) -> bool:
        """True for an authentication failure (HTTP 401) — typically a session
        that expired AND could not be silently re-established. Not a deliberate
        business rejection, so fallback-eligible for 'auto' ops (403 = permission
        denial is deliberate and stays NON-eligible)."""
        return self.http_status == 401

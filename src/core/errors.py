"""Core error types (OpenStack edition). No management-layer symbols here."""
from __future__ import annotations


class BackendError(Exception):
    """A backend call failed. http_status is the HTTP status when known
    (None for connection-level failures); body is a raw response snippet."""

    def __init__(self, message: str, http_status: int | None = None,
                 body: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body

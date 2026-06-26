"""Backend error type for the OpenStack MCP."""
from __future__ import annotations


class OpenStackError(Exception):
    """An OpenStack API / SDK failure surfaced to the caller. `http_status` is the
    HTTP status when known (None for connection-level failures)."""

    def __init__(self, message: str, http_status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body

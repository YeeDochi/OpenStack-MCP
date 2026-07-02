"""OpenStack-only tool factories. A spec dict -> an MCP tool callable.
No routing here: core is a single, self-complete OpenStack server."""
from __future__ import annotations

import inspect
import typing

from mcp.server.fastmcp import Context

from core.context import os_conn
from core.projection import full, summary, cap


def _envelope(data):
    return {"backend": "openstack", "backend_reason": "openstack_only", "data": data}


def make_list(spec):
    fields = spec.get("fields")

    def _list(ctx: Context, all_projects: bool = False, detail: bool = False, limit: int = 0) -> dict:
        conn = os_conn(ctx)
        if conn is None:
            raise RuntimeError("no OpenStack credentials")
        proj = (lambda x: full(x)) if (detail or not fields) else (lambda x: summary(x, fields))
        items = spec["os_list"](conn, all_projects)
        return _envelope([proj(x) for x in cap(items, limit)])
    return _list


def make_show(spec):
    def _show(ctx: Context, resource_id: str) -> dict:
        conn = os_conn(ctx)
        if conn is None:
            raise RuntimeError("no OpenStack credentials")
        return _envelope(full(spec["os_show"](conn, resource_id)))
    return _show


def make_delete(spec):
    """Delete with a human elicitation gate (confirm/cancel, no default) — OpenStack
    only, no routing/agent_mode here (that's the management layer's job)."""
    name = spec["name"]
    label = name.replace("_", " ")

    async def _delete(ctx: Context, resource_id: str) -> dict:
        from pydantic import create_model, Field
        conn = os_conn(ctx)
        if conn is None:
            raise RuntimeError("no OpenStack credentials")
        if not spec.get("os_delete"):
            raise RuntimeError(f"no OpenStack delete implementation for {name}")
        # best-effort: what is being deleted (don't block delete if lookup fails)
        disp = resource_id
        try:
            if spec.get("os_show"):
                disp = full(spec["os_show"](conn, resource_id)).get("name") or resource_id
        except Exception:
            pass
        Confirm = create_model("ConfirmDelete", choice=(str, Field(
            ..., description=f"DELETE {label} '{disp}' (id={resource_id})? 되돌릴 수 없음.",
            json_schema_extra={"enum": ["delete", "cancel"],
                               "enumNames": ["삭제 확정", "취소"]})))
        res = await ctx.elicit(message=f"{label} 삭제 확인", schema=Confirm)
        if (getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None
                or res.data.model_dump().get("choice") != "delete"):
            return {"cancelled": True, "type": name, "id": resource_id}
        spec["os_delete"](conn, resource_id)
        return _envelope({"deleted": resource_id})
    return _delete


def make_update(spec):
    """Partial update (only the passed fields change) — OpenStack only, no routing."""
    name = spec["name"]
    flds = spec["update_fields"]

    def _update(ctx: Context, resource_id: str, **kwargs) -> dict:
        conn = os_conn(ctx)
        if conn is None:
            raise RuntimeError("no OpenStack credentials")
        if not spec.get("os_update"):
            raise RuntimeError(f"no OpenStack update implementation for {name}")
        body = {k: v for k, v in kwargs.items() if k in flds and v is not None and v != ""}
        if not body:
            raise RuntimeError("수정할 필드를 하나 이상 넘기세요 (넘긴 필드만 변경).")
        return _envelope(full(spec["os_update"](conn, resource_id, body)))

    # 명명 옵션 인자를 시그니처에 노출 → FastMCP 스키마에 필드가 뜬다.
    P = inspect.Parameter
    params = [P("ctx", P.POSITIONAL_OR_KEYWORD, annotation=Context),
              P("resource_id", P.POSITIONAL_OR_KEYWORD, annotation=str)]
    params += [P(f, P.KEYWORD_ONLY, default=None, annotation=typing.Optional[str]) for f in flds]
    _update.__signature__ = inspect.Signature(params, return_annotation=dict)
    return _update

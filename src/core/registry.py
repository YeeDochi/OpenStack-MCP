"""Domain/tier tool registry + the shared resource-registration loop.

register_resources() is the single place the spec table becomes tools; both the
core (os-only factories) and the management layer (routing factories) drive it,
so the two editions never diverge in what gets registered."""
from __future__ import annotations

from core.factories import make_list as _make_list, make_show as _make_show
from core.specs import RESOURCE_DOMAIN

DOMAINS = ["compute", "network", "lbaas", "storage", "image", "identity",
           "keymanager", "observability", "billing", "orchestration"]
TIERS = ["read", "write", "maintain"]


class Registry:
    def __init__(self):
        self.tools: list[dict] = []
        self.prompts: list[dict] = []

    def add(self, fn, *, name, description, domain, tier="read"):
        self.tools.append(dict(fn=fn, name=name, description=description,
                               domain=domain, tier=tier))

    def add_prompt(self, prompt, *, domain):
        self.prompts.append(dict(prompt=prompt, domain=domain))


def register_resources(reg, specs, *, make_list=_make_list, make_show=_make_show,
                       make_delete=None, make_update=None, make_parent_list=None,
                       has_delete=lambda spec: bool(spec.get("os_delete")),
                       has_show=lambda spec: bool(spec.get("os_show")),
                       is_parent_scoped=lambda spec: False,
                       kind_note=lambda spec: ""):
    """has_delete / has_show / is_parent_scoped are predicates so a management layer
    built on top of core can recognize its own extra spec fields (e.g. a
    routing-only show/delete path or a parent-list path — a value-add resource with
    no raw-backend equivalent may carry a management-layer show path but no
    os_show) without core ever needing to know their names — keeps core
    grep-clean."""
    for spec in specs:
        dom = spec.get("domain") or RESOURCE_DOMAIN[spec["name"]]
        tier = spec.get("tier", "read")
        pretty = spec["name"].replace("_", " ")
        note = kind_note(spec)
        suffix = (" " + note) if note else ""
        # Parent-scoped list (one id -> a list): replaces the normal list for this
        # spec entirely (e.g. a server's action-history log, a pool's members).
        if make_parent_list is not None and is_parent_scoped(spec):
            pd = spec.get("parent_desc", "parent resource")
            desc = spec.get("desc", f"List {pretty}s")
            reg.add(make_parent_list(spec), name=f"{spec['name']}_list", domain=dom, tier=tier,
                    description=f"{desc} for a {pd} (pass its id as resource_id).{suffix}")
            continue
        reg.add(make_list(spec), name=f"{spec['name']}_list", domain=dom, tier=tier,
                description=(f"List {pretty}s (current project by default). "
                             f"Returns key columns only; pass detail=True for all fields "
                             f"(or use {spec['name']}_show). limit=N caps rows (0=all)."
                             f"{suffix}"))
        if has_show(spec):
            reg.add(make_show(spec), name=f"{spec['name']}_show", domain=dom, tier=tier,
                    description=f"Show one {pretty} by id.{suffix}")
        if make_delete is not None and has_delete(spec):
            reg.add(make_delete(spec), name=f"{spec['name']}_delete", domain=dom, tier="write",
                    description=(f"Delete one {pretty} by id. Asks for human confirmation "
                                 f"(confirm/cancel) before deleting — irreversible.{suffix}"))
        if make_update is not None and spec.get("update_fields"):
            reg.add(make_update(spec), name=f"{spec['name']}_update", domain=dom, tier="write",
                    description=(f"Update one {pretty} by id (partial — only passed fields "
                                 f"change). Updatable: {', '.join(spec['update_fields'])}."
                                 f"{suffix}"))

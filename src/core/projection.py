"""Response projection: full field view, summary view, row cap. Backend-agnostic."""
from __future__ import annotations

DROP_KEYS = {"location", "links", "_csrf", "cpuinfo"}


def full(item):
    try:
        d = item if isinstance(item, dict) else item.to_dict()
    except Exception:
        d = dict(item) if isinstance(item, dict) else {"value": str(item)}
    out = {}
    for k, v in d.items():
        if k in DROP_KEYS or v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def summary(item, fields):
    f = full(item)
    if not fields:
        return f
    picked = {k: f[k] for k in fields if k in f}
    return picked or f


def cap(xs, limit):
    xs = list(xs)
    return xs[:limit] if (limit and limit > 0) else xs

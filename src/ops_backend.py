"""Ops/observability — read OpenStackit & OpenStack (Kolla) logs from host log
files mounted read-only into the server. No podman/privileged access needed.

Mount on the host:  -v /var/log/openstackit:/var/log/openstackit:ro
                    -v /var/log/kolla:/var/log/kolla:ro
"""
from __future__ import annotations

import gzip
import os
import re
import socket
from datetime import datetime, timedelta

import requests

OPIT_LOG_DIR = os.environ.get("OPENSTACKIT_LOG_DIR", "/var/log/openstackit")
KOLLA_LOG_DIR = os.environ.get("KOLLA_LOG_DIR", "/var/log/kolla")
# Which node these logs come from. Logs are node-local (no central store), so in a
# multi-node deploy each node runs its own observability slice; this label tells
# the caller whose logs they're reading. Override with MCP_NODE_NAME, else hostname.
NODE_NAME = os.environ.get("MCP_NODE_NAME") or socket.gethostname()


def _parse_log_nodes(raw: str) -> dict:
    """'c1=http://c1:8011 c2=http://c2:8011' → {'c1': 'http://c1:8011', ...}."""
    out = {}
    for kv in (raw or "").split():
        if "=" in kv:
            node, url = kv.split("=", 1)
            node, url = node.strip(), url.strip().rstrip("/")
            if node and url:
                out[node] = url
    return out


# Other nodes' log endpoints (node → base url). The base/VIP MCP proxies log
# requests here; empty in single-node deploys (local-only).
_LOG_NODES = _parse_log_nodes(os.environ.get("LOG_NODES", ""))


class LogNodeError(Exception):
    """A registered log node could not be reached or returned a bad response.
    Raised (not returned) so node failures surface through the same
    {error:{type,message}} envelope as a bad target (ValueError) — clients
    detect failure uniformly instead of one path raising and another returning
    a success result with an `error` key."""


def _unknown_node(node: str) -> "ValueError":
    """Bad node name = caller mistake, same class as a bad target. English +
    raised so the shape matches; carries the known list + where to look."""
    known = ", ".join(sorted(_LOG_NODES)) or "none"
    return ValueError(f"unknown node '{node}' (known: {known}; see log_targets)")


def _proxy_get(base_url: str, path: str, params: dict | None = None) -> dict:
    """GET base_url+path on a remote node's lightweight log EP. Returns the parsed
    JSON dict; raises LogNodeError on timeout / non-200 / network error / non-JSON."""
    try:
        r = requests.get(base_url + path, params=params or {}, timeout=10)
    except requests.RequestException as e:
        raise LogNodeError(f"node unreachable: {base_url} ({e})") from e
    if r.status_code != 200:
        raise LogNodeError(f"node responded {r.status_code}: {base_url}{path} — {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise LogNodeError(f"node returned non-JSON: {base_url}{path}") from e


def targets_for(node: str = "") -> dict:
    """Node-aware log_targets. node='' → registry list + this node's local targets;
    node=<name> → that node's targets via proxy. Unknown node / unreachable node
    raise (ValueError / LogNodeError) — see module errors note."""
    if not node:
        return {"nodes": sorted(_LOG_NODES), "local": targets()}
    if node not in _LOG_NODES:
        raise _unknown_node(node)
    return _proxy_get(_LOG_NODES[node], "/obs/targets")


def tail_for(target: str, lines: int = 300, grep: str = "", node: str = "",
             since: str = "", until: str = "", last: str = "") -> dict:
    """Node-aware log_tail. node='' → local tail; node=<name> → proxy to that node.
    Unknown node / unreachable node raise (ValueError / LogNodeError)."""
    if not node:
        return tail(target, lines=lines, grep=grep, since=since, until=until, last=last)
    if node not in _LOG_NODES:
        raise _unknown_node(node)
    return _proxy_get(_LOG_NODES[node], "/obs/logs",
                      {"target": target, "lines": lines, "grep": grep,
                       "since": since, "until": until, "last": last})


def _under(path: str, base: str) -> bool:
    """True iff realpath(path) is base itself or strictly inside base — blocks
    '../' traversal out of the log dirs."""
    rp, rb = os.path.realpath(path), os.path.realpath(base)
    return rp == rb or rp.startswith(rb + os.sep)


def targets() -> dict:
    """Available log targets: OpenStackit app logs (typically agent/batch/servlet —
    servlet.log is the WAS app log) and Kolla per-service dirs
    (nova/neutron/keystone/...). Names are derived from the actual *.log files present."""
    out = {"openstackit": [], "kolla": []}
    if os.path.isdir(OPIT_LOG_DIR):
        out["openstackit"] = sorted(f[:-4] for f in os.listdir(OPIT_LOG_DIR)
                                    if f.endswith(".log") and not f.endswith((".gz",)))
    if os.path.isdir(KOLLA_LOG_DIR):
        out["kolla"] = sorted(d for d in os.listdir(KOLLA_LOG_DIR)
                              if os.path.isdir(os.path.join(KOLLA_LOG_DIR, d)))
    out["node"] = NODE_NAME
    return out


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)")
_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_REQ_RE = re.compile(r"\breq-[0-9a-f][0-9a-f-]{7,}\b")
_TRACE_RE = re.compile(r"trace=([0-9a-fA-F][0-9a-fA-F-]{7,})")
_USER_RE = re.compile(r"\buser=([^\]\s]+)")
# 타임스탬프 없는 줄이 실제 연속줄(스택 트레이스)인지 판별.
# 선행 공백, Java 스택 프레임, Python Traceback 패턴 등.
_CONT_RE = re.compile(r"^(\s|at\s|Caused by:|\.{3}|Traceback|[A-Z][A-Za-z]+(?:Error|Exception|Warning):)")
# 레코드 1개 msg 길이 상한 — libvirt 도메인 XML·대형 스택트레이스가 연속줄로 붙어
# 레코드 하나를 통째로 MB 단위로 키우는 걸 막는다. 넘으면 이후 연속줄은 버리고 잘림 표시.
_MAX_MSG_CHARS = 8000


def _parse_line(raw: str, source: str) -> dict:
    """로그 한 줄 → 구조화 레코드. 타임스탬프 없으면 ts=None(연속줄/비정형)."""
    raw = raw.rstrip("\n")
    mts = _TS_RE.match(raw)
    mlv = _LEVEL_RE.search(raw)
    mrq = _REQ_RE.search(raw)
    mtr = _TRACE_RE.search(raw)
    mus = _USER_RE.search(raw)
    user = mus.group(1) if mus else None
    if user == "-":
        user = None
    ident = mrq.group(0) if mrq else (mtr.group(1) if (mtr and mtr.group(1) != "-") else None)
    return {"ts": mts.group(1) if mts else None,
            "level": mlv.group(1) if mlv else None,
            "id": ident, "user": user, "source": source, "msg": raw}


_LAST_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_ABS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M")


def _parse_last(s: str) -> timedelta:
    m = _LAST_RE.match(s)
    if not m:
        raise ValueError(f"상대시간 형식 불가: '{s}' (예: '30m','2h','1d')")
    return timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


def _parse_abs(s: str, now: datetime) -> datetime:
    s = s.strip()
    for f in _ABS_FORMATS:
        try:
            dt = datetime.strptime(s, f)
            if f.startswith("%H"):  # 시각만 → 오늘 날짜로
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            return dt
        except ValueError:
            continue
    raise ValueError(f"시간 형식 인식 불가: '{s}' (예: '2026-06-25 14:30' 또는 '14:30')")


def _time_window(since: str = "", until: str = "", last: str = "") -> tuple[str, str]:
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    if since or until:
        start = _parse_abs(since, now) if since else now - timedelta(days=1)
        end = _parse_abs(until, now) if until else now
    else:
        delta = _parse_last(last or "30m")
        start, end = now - delta, now
    return start.strftime(fmt), end.strftime(fmt)


def _resolve(target: str) -> list[str]:
    """'openstackit:batch' → [.../batch.log]; 'kolla:nova' → all *.log in nova/;
    'kolla:nova/nova-api.log' → that file. Paths are confined to the log dirs."""
    if ":" not in target:
        raise ValueError("target must be 'openstackit:<name>' or 'kolla:<service>[/<file>]'")
    src, rest = target.split(":", 1)
    rest = rest.strip("/")
    if src == "openstackit":
        fp = os.path.join(OPIT_LOG_DIR, rest if rest.endswith(".log") else rest + ".log")
        return [fp] if os.path.isfile(fp) and _under(fp, OPIT_LOG_DIR) else []
    if src == "kolla":
        base = os.path.join(KOLLA_LOG_DIR, rest)
        if os.path.isfile(base) and _under(base, KOLLA_LOG_DIR):
            return [base]
        if os.path.isdir(base) and _under(base, KOLLA_LOG_DIR):
            return sorted(os.path.join(base, f) for f in os.listdir(base) if f.endswith(".log"))
        return []
    raise ValueError("source must be 'openstackit' or 'kolla'")


def _in_window(ts: "str|None", start: str, end: str) -> bool:
    """ts 문자열은 'YYYY-MM-DD HH:MM:SS[.mmm]', 고정폭이라 문자열 비교=시간 비교.
    연속줄(ts=None)은 직전 줄에 병합되고, 연속줄 아닌 단독 None은 호출부(_read_records)에서 드롭."""
    if ts is None:
        return True
    return start <= ts[:len(end)] and ts[:len(start)] <= end


def _read_records(files, grep="", start="0", end="9", id_sub=""):
    """파일들을 스트리밍 파싱, 연속줄은 직전 레코드 msg에 병합, [start,end] 윈도우 + 선택적
    grep 정규식 + 선택적 id_sub 부분일치로 필터. 결과: list[dict].

    타임스탬프 없는 줄(uwsgi/access 로그 등)은 _CONT_RE로 실제 연속줄인지 판별:
    - 선행공백·스택 프레임 패턴 → 직전 레코드에 병합(스택 트레이스 보존)
    - 그 외(uwsgi 등 비정형 독립줄) → 시간 배치 불가, 드롭(윈도우 필터 정합성 유지)
    """
    pat = re.compile(grep) if grep else None
    out = []
    for fp in files:
        src = os.path.basename(fp)
        opener = gzip.open if fp.endswith(".gz") else open
        try:
            with opener(fp, "rt", errors="replace") as f:
                for line in f:
                    rec = _parse_line(line, src)
                    if rec["ts"] is None:
                        # 실제 연속줄(스택 트레이스)만 직전 레코드에 병합 — 레코드당 길이 상한 적용
                        if out and _CONT_RE.match(rec["msg"]):
                            prev = out[-1]
                            if len(prev["msg"]) < _MAX_MSG_CHARS:
                                prev["msg"] += "\n" + rec["msg"]
                                if len(prev["msg"]) >= _MAX_MSG_CHARS:
                                    prev["msg"] += "\n… (이하 연속줄 생략 — 레코드 길이 상한)"
                            # 이미 상한 도달 → 이후 연속줄 드롭(거대 XML/스택 폭주 방지)
                        # 그 외 비정형 독립줄(uwsgi 등) — 시간 배치 불가, 드롭
                        continue
                    if not _in_window(rec["ts"], start, end):
                        continue
                    if pat is not None and not pat.search(rec["msg"]):
                        continue
                    if id_sub and id_sub not in rec["msg"]:
                        continue
                    # grep/id 매칭은 풀 내용에 끝났으니, 출력용으로만 단일 라인 길이 상한 적용
                    # (POST /v2.1/servers·vif 이벤트처럼 한 줄에 libvirt XML이 통째로 박힌 경우)
                    if len(rec["msg"]) > _MAX_MSG_CHARS:
                        rec["msg"] = rec["msg"][:_MAX_MSG_CHARS] + "\n… (라인 길이 상한 — XML 등 생략)"
                    out.append(rec)
        except OSError as e:
            out.append({"ts": None, "level": "ERROR", "id": None, "user": None,
                        "source": src, "msg": f"(읽기 실패 {src}: {e})"})
    return out


def _merge_cap(records, cap=2000, cap_bytes=512000):
    """ts(None은 맨 뒤) 기준 정렬, 줄 수/바이트 상한 적용.
    (records, truncated, cursor) 반환. cursor=마지막 레코드 ts."""
    records.sort(key=lambda r: (r["ts"] is None, r["ts"] or ""))
    truncated = False
    if len(records) > cap:
        records = records[-cap:]            # 최신 우선 유지
        truncated = True
    total = 0
    for i, r in enumerate(records):
        total += len(r["msg"].encode("utf-8", "replace"))
        if total > cap_bytes:
            records = records[i:]
            truncated = True
            break
    cursor = next((r["ts"] for r in reversed(records) if r["ts"]), None)
    return records, truncated, cursor


def tail(target: str, lines: int = 300, grep: str = "",
         since: str = "", until: str = "", last: str = "") -> dict:
    """target 로그를 시간창 안에서 파싱·병합정렬해 구조화 레코드로 반환.
    시간 인자(since/until/last)가 모두 비면 기본 last=30m. lines는 상한(cap)으로 쓰인다."""
    files = _resolve(target)
    if not files:
        raise ValueError(f"no log files for target '{target}' (see log_targets)")
    start, end = _time_window(since, until, last)
    recs = _read_records(files, grep=grep, start=start, end=end)
    recs, truncated, cursor = _merge_cap(recs, cap=max(1, min(lines, 5000)))
    return {"node": NODE_NAME, "target": target, "window": {"since": start, "until": end},
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}


def _all_target_names() -> list:
    t = targets()
    names = [f"openstackit:{n}" for n in t.get("openstackit", [])]
    names += [f"kolla:{s}" for s in t.get("kolla", [])]
    return names


def trace_for(id_sub: str, since: str = "", until: str = "", last: str = "",
              nodes: str = "", targets_csv: str = "") -> dict:
    """id_sub를 로컬+원격 노드에서 팬아웃 검색, 타임스탬프 기준으로 머지.
    nodes: ''=로컬만 · 'all'=_LOG_NODES 전체 · 'c1,c2'=지정.
    각 노드는 per-node status(ok/unknown/unreachable:…)로 보고, 전체 실패 없음."""
    local = trace(id_sub, since=since, until=until, last=last, targets_csv=targets_csv)
    window = local["window"]
    statuses = {NODE_NAME: "ok"}
    recs = list(local["records"])
    if nodes == "all":
        sel = list(_LOG_NODES)
    elif nodes:
        sel = [n.strip() for n in nodes.split(",") if n.strip()]
    else:
        sel = []
    for n in sel:
        if n not in _LOG_NODES:
            statuses[n] = "unknown"
            continue
        try:
            data = _proxy_get(_LOG_NODES[n], "/obs/trace",
                              {"id": id_sub, "since": window["since"], "until": window["until"],
                               "targets": targets_csv})
            node_recs = data.get("records", [])
            for r in node_recs:
                r["node"] = n
            recs += node_recs
            statuses[n] = "ok"
        except Exception as e:                       # 도달 불가 노드는 표시만, 전체는 계속
            statuses[n] = f"unreachable: {type(e).__name__}"
    recs, truncated, cursor = _merge_cap(recs)
    return {"id": id_sub, "window": window, "nodes": statuses,
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}


def trace(id_sub: str, since: str = "", until: str = "", last: str = "",
          targets_csv: str = "") -> dict:
    """id_sub(req-... 또는 trace uuid)를 이 노드의 전(또는 지정) 서비스 로그에서 시간창 안에 모은다."""
    if not id_sub or not id_sub.strip():
        raise ValueError("trace id가 비었습니다")
    start, end = _time_window(since, until, last)
    names = [s.strip() for s in targets_csv.split(",") if s.strip()] or _all_target_names()
    recs = []
    for name in names:
        try:
            recs += _read_records(_resolve(name), start=start, end=end, id_sub=id_sub)
        except ValueError:
            continue   # 없는 target 이름은 건너뜀(전체 열거 시 안전)
    recs, truncated, cursor = _merge_cap(recs)
    for r in recs:
        r["node"] = NODE_NAME
    return {"node": NODE_NAME, "id": id_sub, "window": {"since": start, "until": end},
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}

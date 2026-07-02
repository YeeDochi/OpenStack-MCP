"""Observability — read OpenStack (Kolla) logs from host log files mounted
read-only into the server. No podman/privileged access needed.

Mount on the host:  -v /var/log/kolla:/var/log/kolla:ro

Generic parsing/windowing/proxy building blocks live here so a management
layer built on top of core (which may add further log sources / id-correlation
schemes) can reuse them via the `req_ids_fn` / `resolve` / `all_target_names` /
`extra_id_res` hooks instead of re-implementing them — see trace()/_read_records().
"""
from __future__ import annotations

import gzip
import os
import re
import socket
from datetime import datetime, timedelta, timezone

import requests

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


def trace_for(id_sub: str, since: str = "", until: str = "", last: str = "",
              nodes: str = "", targets_csv: str = "", link_by: str = "none") -> dict:
    """Node-aware log_trace. Fans out to local + registered nodes, merges by
    timestamp. nodes: ''=local only · 'all'=every registered node · 'c1,c2'=selected.
    Each node reports its own status (ok/unknown/unreachable:…); no all-or-nothing
    failure."""
    local = trace(id_sub, since=since, until=until, last=last,
                  targets_csv=targets_csv, link_by=link_by)
    window = local["window"]
    statuses = {NODE_NAME: "ok"}
    recs = list(local["records"])
    linked = set(local.get("linked_ids", []))
    dropped_noise = local.get("dropped_monitor_noise", 0)
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
                               "targets": targets_csv, "link_by": link_by})
            node_recs = data.get("records", [])
            for r in node_recs:
                r["node"] = n
            recs += node_recs
            linked.update(data.get("linked_ids", []))
            dropped_noise += data.get("dropped_monitor_noise", 0)
            statuses[n] = "ok"
        except Exception as e:                       # 도달 불가 노드는 표시만, 전체는 계속
            statuses[n] = f"unreachable: {type(e).__name__}"
    recs, truncated, cursor = _merge_cap(recs)
    return {"id": id_sub, "linked_ids": sorted(linked), "window": window, "nodes": statuses,
            "dropped_monitor_noise": dropped_noise,
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}


def _under(path: str, base: str) -> bool:
    """True iff realpath(path) is base itself or strictly inside base — blocks
    '../' traversal out of the log dirs."""
    rp, rb = os.path.realpath(path), os.path.realpath(base)
    return rp == rb or rp.startswith(rb + os.sep)


def _scan_log_names(dirpath: str) -> list[str]:
    """List *.log basenames (no extension, no .gz) directly under dirpath."""
    if not os.path.isdir(dirpath):
        return []
    return sorted(f[:-4] for f in os.listdir(dirpath)
                 if f.endswith(".log") and not f.endswith((".gz",)))


def targets() -> dict:
    """Available log targets: Kolla per-service dirs (nova/neutron/keystone/...).
    Names are derived from the actual *.log files present."""
    out = {"kolla": []}
    if os.path.isdir(KOLLA_LOG_DIR):
        out["kolla"] = sorted(d for d in os.listdir(KOLLA_LOG_DIR)
                              if os.path.isdir(os.path.join(KOLLA_LOG_DIR, d)))
    out["node"] = NODE_NAME
    return out


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)")
_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_REQ_RE = re.compile(r"\breq-[0-9a-f][0-9a-f-]{7,}\b")
_USER_RE = re.compile(r"\buser=([^\]\s]+)")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# instance/server UUID extraction for cross-service correlation (link_by=entity).
# Any bare UUID (project/user/image) would over-expand, so only UUIDs that follow
# an instance/server context are matched: nova `[instance: <id>]`, REST `servers/<id>`.
_INSTANCE_ID_RE = re.compile(
    r"(?:instance[:=]\s*|instance\s+|servers/)(" + _UUID + r")")
# oslo context brackets: [<req-global or None> <req-local> <user> <project> - - default default].
# 1st = global_request_id (propagates across services, the work-unit key), 2nd = this
# service's local request_id. If the request originates here (no upstream global),
# 1st is None and 2nd (=this service's own request_id) is the work key. i.e. "the
# first req- token" is the work key either way: [None req-X]→X, [req-G req-L]→G.
_CTX_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
# entity pass-2 noise cut. Dashboards/health pollers reuse a fixed req-id
# (X-Openstack-Request-Id); if that req- co-occurs even once with the entity, the
# pass-2 sweep sucks in unrelated list/health GETs. Real build GETs always carry a
# specific resource UUID (servers/{id}, images/{id}/file, ports?device_id={id});
# ops lines aren't access-log lines to begin with. So: an access-log GET whose path
# has no UUID at all (pure list/health poll) is dropped only from entity expansion.
_ACCESS_GET_PATH_RE = re.compile(r'"GET\s+(\S+)')
_UUID_RE = re.compile(_UUID)
# Whether a timestamp-less line is a real continuation line (stack trace): leading
# whitespace, Java stack frames, Python Traceback patterns etc.
_CONT_RE = re.compile(r"^(\s|at\s|Caused by:|\.{3}|Traceback|[A-Z][A-Za-z]+(?:Error|Exception|Warning):)")
# Response-body XML/tag lines (<host>, <uuid> …) have leading whitespace so they'd
# match _CONT_RE (^\s) and get merged into the wrong preceding record (e.g. a libvirt
# capabilities <capabilities> dump — 1000+ lines → one bloated, token-flooded record).
# Tag lines are excluded from continuation handling (dropped).
_XML_LINE_RE = re.compile(r"^\s*<")
# Per-record msg length cap — a libvirt domain XML or huge stack trace merged in as
# continuation lines could otherwise grow one record to MB size. Past the cap, later
# continuation lines are dropped and the record is marked truncated.
_MAX_MSG_CHARS = 8000


def _req_ids(raw: str):
    """oslo 줄에서 (req_local, req_global) 추출. global=전파되는 작업 키(첫 req-).
    선행 대괄호 req- 2개 [global local]→(local=2nd, global=1st) · 1개 [None global]→(None, it) ·
    0개→대괄호 밖 첫 req- 폴백. 둘 다 없으면 (None, None)."""
    mb = _CTX_BRACKET_RE.search(raw)
    reqs = _REQ_RE.findall(mb.group(1)) if mb else []
    if len(reqs) >= 2:
        return reqs[1], reqs[0]   # [global local] → (local, global)
    if len(reqs) == 1:
        return None, reqs[0]
    m = _REQ_RE.search(raw)
    if m:
        return None, m.group(0)
    return None, None


def _parse_line(raw: str, source: str, req_ids_fn=_req_ids) -> dict:
    """로그 한 줄 → 구조화 레코드. 타임스탬프 없으면 ts=None(연속줄/비정형).
    req_ids_fn은 교체 가능한 훅 — 상위(management layer)에서 추가 id 체계를
    붙이고 싶을 때 core 파싱을 그대로 재사용하며 훅만 바꾼다."""
    raw = raw.rstrip("\n")
    mts = _TS_RE.match(raw)
    mlv = _LEVEL_RE.search(raw)
    mus = _USER_RE.search(raw)
    user = mus.group(1) if mus else None
    if user == "-":
        user = None
    req_local, req_global = req_ids_fn(raw)
    return {"ts": mts.group(1) if mts else None,
            "level": mlv.group(1) if mlv else None,
            "id": req_global, "req_local": req_local, "req_global": req_global,
            "user": user, "source": source, "msg": raw}


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


_FMT = "%Y-%m-%d %H:%M:%S"


def _local_utc_offset() -> timedelta:
    """호스트 로컬 - UTC (KST면 ≈ +9h). 초 단위로 반올림(µs 레이스 제거)."""
    utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    off = datetime.now() - utc_naive
    return timedelta(seconds=round(off.total_seconds()))


def _windows(since: str = "", until: str = "", last: str = "") -> tuple[str, str, tuple]:
    """표시용 로컬 윈도우 (start,end) + 매칭용 win 튜플을 함께 만든다.
    로그 소스마다 TZ가 다를 수 있다(kolla=UTC). 한쪽 TZ로만 윈도우를 만들면 다른
    쪽이 통째로 0건이 되므로, 같은 실시각 구간을 로컬·UTC 두 표현으로 들고 다니다
    레코드가 둘 중 하나라도 들면 통과시킨다(union). UTC 윈도우에만 걸린 레코드는
    UTC 타임스탬프이므로 출력 시 +off 해서 로컬로 통일(정렬·표시 일관)."""
    start, end = _time_window(since, until, last)
    off = _local_utc_offset()
    us = (datetime.strptime(start, _FMT) - off).strftime(_FMT)
    ue = (datetime.strptime(end, _FMT) - off).strftime(_FMT)
    return start, end, (start, end, us, ue, off)


def _shift_ts(ts: str, off: timedelta) -> str:
    """UTC 타임스탬프 문자열을 로컬로 (+off). '.mmm' 보존, 파싱 실패 시 원본 유지."""
    base, dot, ms = ts.partition(".")
    try:
        s = (datetime.strptime(base, _FMT) + off).strftime(_FMT)
    except ValueError:
        return ts
    return f"{s}.{ms}" if dot else s


def _resolve(target: str) -> list[str]:
    """'kolla:nova' → all *.log in nova/; 'kolla:nova/nova-api.log' → that file.
    Paths are confined to KOLLA_LOG_DIR."""
    if ":" not in target:
        raise ValueError("target must be 'kolla:<service>[/<file>]'")
    src, rest = target.split(":", 1)
    rest = rest.strip("/")
    if src == "kolla":
        base = os.path.join(KOLLA_LOG_DIR, rest)
        if os.path.isfile(base) and _under(base, KOLLA_LOG_DIR):
            return [base]
        if os.path.isdir(base) and _under(base, KOLLA_LOG_DIR):
            return sorted(os.path.join(base, f) for f in os.listdir(base) if f.endswith(".log"))
        return []
    raise ValueError("source must be 'kolla'")


def _in_window(ts: "str|None", start: str, end: str) -> bool:
    """ts 문자열은 'YYYY-MM-DD HH:MM:SS[.mmm]', 고정폭이라 문자열 비교=시간 비교.
    연속줄(ts=None)은 직전 줄에 병합되고, 연속줄 아닌 단독 None은 호출부(_read_records)에서 드롭."""
    if ts is None:
        return True
    return start <= ts[:len(end)] and ts[:len(start)] <= end


def _read_records(files, grep="", win=None, id_subs=None, req_ids_fn=_req_ids):
    """파일들을 스트리밍 파싱, 연속줄은 직전 레코드 msg에 병합, [start,end] 윈도우 + 선택적
    grep 정규식 + 선택적 id_subs(부분일치 집합, 하나라도 매치하면 통과)로 필터. 결과: list[dict].

    req_ids_fn은 _parse_line에 그대로 전달되는 훅 — 상위 레이어가 추가 id 체계(예:
    다른 correlation id 포맷)를 붙일 때 이 함수 전체를 재구현하지 않고 훅만 바꿔
    재사용한다(단일 소스).

    타임스탬프 없는 줄(uwsgi/access 로그 등)은 _CONT_RE로 실제 연속줄인지 판별:
    - 선행공백·스택 프레임 패턴 → 직전 레코드에 병합(스택 트레이스 보존)
    - 그 외(uwsgi 등 비정형 독립줄) → 시간 배치 불가, 드롭(윈도우 필터 정합성 유지)
    """
    pat = re.compile(grep) if grep else None
    lstart, lend, ustart, uend, off = win or ("0", "9", "0", "9", timedelta(0))
    out = []
    for fp in files:
        src = os.path.basename(fp)
        opener = gzip.open if fp.endswith(".gz") else open
        try:
            with opener(fp, "rt", errors="replace") as f:
                for line in f:
                    rec = _parse_line(line, src, req_ids_fn=req_ids_fn)
                    if rec["ts"] is None:
                        # 실제 연속줄(스택 트레이스)만 직전 레코드에 병합 — 레코드당 길이 상한 적용.
                        # XML/태그 줄(^\s*<)은 응답 본문 덤프라 연속줄에서 제외(아래서 드롭).
                        if out and _CONT_RE.match(rec["msg"]) and not _XML_LINE_RE.match(rec["msg"]):
                            prev = out[-1]
                            if len(prev["msg"]) < _MAX_MSG_CHARS:
                                prev["msg"] += "\n" + rec["msg"]
                                if len(prev["msg"]) >= _MAX_MSG_CHARS:
                                    prev["msg"] += "\n… (이하 연속줄 생략 — 레코드 길이 상한)"
                            # 이미 상한 도달 → 이후 연속줄 드롭(거대 XML/스택 폭주 방지)
                        # 그 외 비정형 독립줄(uwsgi 등) — 시간 배치 불가, 드롭
                        continue
                    # union: 로컬 윈도우 우선, 거기 안 들면 UTC 윈도우. UTC에만 걸리면
                    # UTC ts이므로 +off로 로컬 정규화(섞인 소스의 정렬·표시 일관성).
                    if _in_window(rec["ts"], lstart, lend):
                        pass
                    elif _in_window(rec["ts"], ustart, uend):
                        old = rec["ts"]
                        rec["ts"] = _shift_ts(old, off)
                        # msg 앞머리 raw ts도 같이 로컬로 — 구조화 ts와 본문이 어긋나지 않게
                        if rec["msg"].startswith(old):
                            rec["msg"] = rec["ts"] + rec["msg"][len(old):]
                    else:
                        continue
                    if pat is not None and not pat.search(rec["msg"]):
                        continue
                    if id_subs and not any(s in rec["msg"] for s in id_subs):
                        continue
                    # grep/id 매칭은 풀 내용에 끝났으니, 출력용으로만 단일 라인 길이 상한 적용
                    if len(rec["msg"]) > _MAX_MSG_CHARS:
                        rec["msg"] = rec["msg"][:_MAX_MSG_CHARS] + "\n… (라인 길이 상한 — XML 등 생략)"
                    out.append(rec)
        except OSError as e:
            out.append({"ts": None, "level": "ERROR", "id": None, "req_local": None, "req_global": None,
                        "user": None, "source": src, "msg": f"(읽기 실패 {src}: {e})"})
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
    start, end, win = _windows(since, until, last)
    recs = _read_records(files, grep=grep, win=win)
    recs, truncated, cursor = _merge_cap(recs, cap=max(1, min(lines, 5000)))
    return {"node": NODE_NAME, "target": target, "window": {"since": start, "until": end},
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}


def _all_target_names() -> list:
    t = targets()
    return [f"kolla:{s}" for s in t.get("kolla", [])]


def _correlated_ids(records, seed, extra_id_res=()):
    """seed 레코드에서 결합 키 추출: instance/server UUID + 각 줄의 글로벌
    request id(req_global, 작업 단위 키). extra_id_res는 상위 레이어가 추가
    id 포맷(정규식들)을 끼워 넣는 훅 — 각 정규식의 group(1)이 후보 id. 로컬
    sub-request id는 안 잡는다(서비스마다 달라 과대확장). 상한 12개로 과대확장
    방지. seed 포함."""
    ids = {seed}
    for r in records:
        m = r.get("msg", "")
        for mt in _INSTANCE_ID_RE.finditer(m):
            ids.add(mt.group(1))
        if r.get("req_global"):
            ids.add(r["req_global"])
        for extra_re in extra_id_res:
            for mt in extra_re.finditer(m):
                g = mt.group(1)
                if g and g != "-":
                    ids.add(g)
        if len(ids) >= 12:
            break
    return ids


def _drop_monitor_noise(records):
    """entity 확장 결과에서 순수 리스트/헬스 폴 줄을 제거한다(재사용 req-id 누수 컷).
    드롭 조건: 액세스로그 'GET <path>' 줄인데 path에 리소스 UUID가 전혀 없음.
    (path만 검사 — 로그 프리픽스의 req- UUID는 무시.) 드롭 수도 함께 반환해 은닉 안 함."""
    kept, dropped = [], 0
    for r in records:
        g = _ACCESS_GET_PATH_RE.search(r.get("msg", ""))
        if g and not _UUID_RE.search(g.group(1)):
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


def trace(id_sub: str, since: str = "", until: str = "", last: str = "",
          targets_csv: str = "", link_by: str = "none", *,
          resolve=_resolve, all_target_names=_all_target_names,
          req_ids_fn=_req_ids, extra_id_res=()) -> dict:
    """id_sub(req-... 등)를 이 노드의 전(또는 지정) 서비스 로그에서 시간창 안에 모은다.
    link_by='entity'면 1차 결과에서 instance/server UUID·req- id를 뽑아 그 ID들로 한 번 더
    스윕(창은 seed 구간 ±15s로 좁힘)해 여러 서비스에 걸친 한 작업의 전 구간을 한 타임라인에
    엮는다.

    resolve/all_target_names/req_ids_fn/extra_id_res는 교체 가능한 훅 — 상위(management
    layer)가 추가 로그 소스나 추가 id 포맷을 붙이고 싶을 때, 이 함수(윈도우 계산·시드
    스윕·entity 2차 스윕·노이즈 컷·머지)를 재구현하지 않고 훅만 갈아 끼워 재사용한다."""
    if not id_sub or not id_sub.strip():
        raise ValueError("trace id가 비었습니다")
    start, end, win = _windows(since, until, last)
    names = [s.strip() for s in targets_csv.split(",") if s.strip()] or all_target_names()

    def _sweep(id_set, w):
        rs = []
        for name in names:
            try:
                rs += _read_records(resolve(name), win=w, id_subs=id_set, req_ids_fn=req_ids_fn)
            except ValueError:
                continue   # 없는 target 이름은 건너뜀(전체 열거 시 안전)
        return rs

    seed = id_sub.strip()
    recs = _sweep({seed}, win)
    linked = [seed]
    dropped_noise = 0
    if link_by == "entity" and recs:
        ids = _correlated_ids(recs, seed, extra_id_res=extra_id_res)
        if ids - {seed}:
            # pass2 창을 seed 레코드 구간 ±15s로 좁혀 엔티티 UUID의 무관한 출현 과대확장 억제
            seed_ts = sorted(r["ts"] for r in recs if r["ts"])
            w2 = win
            if seed_ts:
                pad = timedelta(seconds=15)
                lo = (datetime.strptime(seed_ts[0][:19], _FMT) - pad).strftime(_FMT)
                hi = (datetime.strptime(seed_ts[-1][:19], _FMT) + pad).strftime(_FMT)
                _, _, w2 = _windows(since=lo, until=hi)
            recs = _sweep(ids, w2)
            # 재사용 req-id로 빨려온 순수 리스트/헬스 폴 줄 컷(entity 확장에서만)
            recs, dropped_noise = _drop_monitor_noise(recs)
            linked = sorted(ids)
    recs, truncated, cursor = _merge_cap(recs)
    for r in recs:
        r["node"] = NODE_NAME
    return {"node": NODE_NAME, "id": id_sub, "linked_ids": linked,
            "window": {"since": start, "until": end}, "dropped_monitor_noise": dropped_noise,
            "returned": len(recs), "truncated": truncated, "cursor": cursor, "records": recs}

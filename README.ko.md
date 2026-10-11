# OpenStack-MCP

[English](README.md) | **한국어**

OpenStack용 Model Context Protocol(MCP) 서버 — 7개 도메인에 걸친 123개 툴을 [openstacksdk](https://docs.openstack.org/openstacksdk/) 위에 구현했으며, 상태를 저장하지 않는 호출자별(per-caller) 헤더 인증, 선언형 RESOURCES 레지스트리, Kolla 로그 관측성(observability)을 제공합니다.

---

## 아키텍처

```
LLM 클라이언트 (Claude / 임의의 MCP 호스트)
        │  MCP 프로토콜 (stdio 또는 HTTP/SSE)
        ▼
┌─────────────────────────────────────────────────────┐
│              src/core/  (패키지)                     │
│                                                     │
│  server.py     → CORE_SPECS 테이블 →                │
│                  list/show/update/delete 툴 자동    │
│                  생성 + 직접 작성한 특수 툴          │
│  registry.py   → 도메인/티어 툴 레지스트리          │
│  assembly.py   → 도메인별 FastMCP 인스턴스, ASGI    │
│                  앱, 공통 안내문                    │
│                                                     │
│  context.os_conn(ctx) ──► os_backend.py            │
│  (호출자별 자격증명)        openstacksdk Connection │
│                            (Keystone 애플리케이션    │
│                             크리덴셜)                │
│                                ▼                    │
│                       OpenStack API                 │
│                       Nova · Neutron · Cinder       │
│                       Glance · Keystone · Octavia   │
│                       Placement                     │
│                                                     │
│  observability.py ──► Kolla 로그 파일 (읽기 전용)   │
│                          /var/log/kolla/*           │
└─────────────────────────────────────────────────────┘

도메인별 HTTP 마운트 (elicitation을 위한 상태 유지 세션):
  /compute/mcp   /network/mcp   /lbaas/mcp
  /storage/mcp   /image/mcp     /identity/mcp
  /observability/mcp
```

각 도메인은 독립적인 FastMCP 인스턴스입니다. 하나의 프로세스가 모든 마운트를 노출하며, `MCP_DOMAINS`와 `MCP_TIERS`로 활성화할 툴 범위를 좁힐 수 있습니다.

---

## 주요 기능

- **선언형 레지스트리** — `CORE_SPECS` 테이블 + `make_list/make_show/make_update/make_delete` 제너레이터. 새 리소스를 추가하려면 딕셔너리 항목 하나만 넣으면 됩니다.
- **상태 비저장 호출자별 인증** — 자격증명을 매 호출마다 요청 헤더(HTTP)나 환경변수(stdio)에서 읽습니다. 서버는 아무것도 저장하지 않으므로, 서로 다른 자격증명을 가진 다수의 호출자가 하나의 프로세스를 안전하게 공유합니다.
- **구조화된 에러 봉투(envelope)** — 모든 툴 에러는 `Error executing tool <name>: {"error":{"type","message","http_status?}}` 형태로 표면화됩니다. 첫 `{`부터 파싱하세요.
- **삭제 확인** — `*_delete` 툴은 MCP elicitation을 사용해 실행 전 사람의 명시적 `"delete"` 선택을 요구합니다. 되돌릴 수 없는 작업은 LLM 단독으로 트리거할 수 없습니다.
- **키 컬럼 / 상세** — list 툴은 기본적으로 간결한 키 컬럼 뷰를 반환합니다. `detail=True`로 전체 필드를, `limit=N`으로 행 수를 제한하고, 지원되는 경우 `all_projects=True`로 관리자 뷰를 볼 수 있습니다.
- **멀티마운트** — 7개의 도메인별 FastMCP 인스턴스를 `/<domain>/mcp`로 서빙하며, 각 인스턴스의 `initialize` 안내문에 라우팅 맵을 담아 클라이언트가 첫 시도에 올바른 마운트를 고르도록 합니다.
- **Kolla 로그 관측성** — `log_targets`, `log_tail`, `log_trace`는 호스트 파일시스템에 (읽기 전용으로) 마운트된 Kolla 서비스 로그 파일을 직접 읽으며, 시간창 필터링, 정규식 grep, 요청 ID 기반 서비스 간(cross-service) 추적을 지원합니다.

---

## 문서

- **[사용법](docs/USAGE.ko.md)** — 설치, stdio·HTTP 모드, 컨테이너, 설정 레퍼런스.
- **[툴 레퍼런스](docs/TOOLS.ko.md)** — 도메인별 123개 툴 전체.

빠른 설치:

```bash
git clone https://github.com/YeeDochi/OpenStack-MCP.git
cd OpenStack-MCP
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

이후 stdio 또는 HTTP 모드로 실행하는 방법은 [사용법](docs/USAGE.ko.md)을 참고하세요.

---

## 확장하기: 새 리소스 추가

Create 툴은 의도적으로 구현하지 않았으며, 이는 주요 확장 지점입니다. create 툴이나 새 리소스 타입을 추가하려면:

1. openstacksdk를 사용해 `src/core/server.py` 또는 `src/core/os_backend.py`에 함수를 추가합니다.
2. `reg.add(fn, name="...", domain="...", tier="write")`로 등록합니다.
3. 완전한 CRUD 리소스라면 `src/core/specs.py`의 `CORE_SPECS`에 딕셔너리 하나를 추가하면, `registry.py`의 `register_resources`가 `os_list`/`os_show`/`os_update`/`os_delete`/`update_fields`를 보고 list/show/update/delete 툴을 자동 생성합니다.

openstacksdk가 지원하는 어떤 OpenStack 서비스든 이런 식으로 몇 줄만에 연결할 수 있습니다.

---

## 테스트 실행

```bash
pytest -q
```

테스트 스위트가 검증하는 것: 레지스트리 조립(툴 구성, 도메인, 티어), list/show/update/delete 팩토리, 삭제 확인 elicitation 흐름, Kolla 로그 백엔드(타깃 해석, 시간창 tail, 요청 ID 추출).

---

## 라이선스

MIT — [LICENSE](LICENSE) 참조.

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
│                  server.py                          │
│  RESOURCES 테이블 → list/show/update/delete 툴      │
│  자동 생성  +  직접 작성한 특수 툴                  │
│                                                     │
│  _os_conn(ctx) ──► os_backend.py                   │
│  (호출자별 자격증명)  openstacksdk Connection       │
│                       (Keystone 애플리케이션 크리덴셜)│
│                                ▼                    │
│                       OpenStack API                 │
│                       Nova · Neutron · Cinder       │
│                       Glance · Keystone · Octavia   │
│                       Placement                     │
│                                                     │
│  ops_backend.py ──► Kolla 로그 파일 (읽기 전용)     │
│  (관측성)             /var/log/kolla/*              │
└─────────────────────────────────────────────────────┘

도메인별 HTTP 마운트 (elicitation을 위한 상태 유지 세션):
  /compute/mcp   /network/mcp   /lbaas/mcp
  /storage/mcp   /image/mcp     /identity/mcp
  /observability/mcp
```

각 도메인은 독립적인 FastMCP 인스턴스입니다. 하나의 프로세스가 모든 마운트를 노출하며, `OSMCP_DOMAINS`와 `OSMCP_TIERS`로 활성화할 툴 범위를 좁힐 수 있습니다.

---

## 주요 기능

- **선언형 레지스트리** — `RESOURCES` 테이블 + `_make_list/_make_show/_make_update/_make_delete` 제너레이터. 새 리소스를 추가하려면 딕셔너리 항목 하나만 넣으면 됩니다.
- **상태 비저장 호출자별 인증** — 자격증명을 매 호출마다 요청 헤더(HTTP)나 환경변수(stdio)에서 읽습니다. 서버는 아무것도 저장하지 않으므로, 서로 다른 자격증명을 가진 다수의 호출자가 하나의 프로세스를 안전하게 공유합니다.
- **구조화된 에러 봉투(envelope)** — 모든 툴 에러는 `Error executing tool <name>: {"error":{"type","message","http_status?}}` 형태로 표면화됩니다. 첫 `{`부터 파싱하세요.
- **삭제 확인** — `*_delete` 툴은 MCP elicitation을 사용해 실행 전 사람의 명시적 `"delete"` 선택을 요구합니다. 되돌릴 수 없는 작업은 LLM 단독으로 트리거할 수 없습니다.
- **키 컬럼 / 상세** — list 툴은 기본적으로 간결한 키 컬럼 뷰를 반환합니다. `detail=True`로 전체 필드를, `limit=N`으로 행 수를 제한하고, 지원되는 경우 `all_projects=True`로 관리자 뷰를 볼 수 있습니다.
- **멀티마운트** — 7개의 도메인별 FastMCP 인스턴스를 `/<domain>/mcp`로 서빙하며, 각 인스턴스의 `initialize` 안내문에 라우팅 맵을 담아 클라이언트가 첫 시도에 올바른 마운트를 고르도록 합니다.
- **Kolla 로그 관측성** — `log_targets`, `log_tail`, `log_trace`는 호스트 파일시스템에 (읽기 전용으로) 마운트된 Kolla 서비스 로그 파일을 직접 읽으며, 시간창 필터링, 정규식 grep, 요청 ID 기반 서비스 간(cross-service) 추적을 지원합니다.

---

## 빠른 시작

### 사전 요구사항

- Python 3.10 이상
- Keystone **애플리케이션 크리덴셜**이 있는 OpenStack 클라우드 (수행할 작업에 따라 프로젝트 또는 도메인 스코프)
- Kolla 로그 툴을 쓰려면 서버 호스트에서 `/var/log/kolla`에 접근 가능해야 함 (또는 컨테이너에 마운트)

### 설치

```bash
git clone https://github.com/YeeDochi/OpenStack-MCP.git
cd OpenStack-MCP
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### stdio 모드 (Claude Desktop / `claude mcp add`)

환경변수를 설정한 뒤 실행:

```bash
export OS_AUTH_URL=https://keystone.example.com:5000/v3
export OS_APPLICATION_CREDENTIAL_ID=<your-app-cred-id>
export OS_APPLICATION_CREDENTIAL_SECRET=<your-app-cred-secret>

python src/server.py --transport stdio
```

Claude Desktop 설정 (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "openstack": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/OpenStack-MCP/src/server.py", "--transport", "stdio"],
      "env": {
        "OS_AUTH_URL": "https://keystone.example.com:5000/v3",
        "OS_APPLICATION_CREDENTIAL_ID": "...",
        "OS_APPLICATION_CREDENTIAL_SECRET": "..."
      }
    }
  }
}
```

또는 CLI로:

```bash
claude mcp add --transport stdio openstack -- /path/to/.venv/bin/python /path/to/OpenStack-MCP/src/server.py
```

### HTTP 모드 (다중 사용자 / 컨테이너)

```bash
# 예시 설정 파일을 복사해 편집
cp config.env.example config.env
# OS_AUTH_URL, MCP_PORT, KOLLA_LOG_DIR 등을 설정

python src/server.py --transport http --host 0.0.0.0 --port 8001
```

도메인별 엔드포인트:

```
http://localhost:8001/compute/mcp
http://localhost:8001/network/mcp
http://localhost:8001/lbaas/mcp
http://localhost:8001/storage/mcp
http://localhost:8001/image/mcp
http://localhost:8001/identity/mcp
http://localhost:8001/observability/mcp
```

매 호출마다 요청 헤더로 자격증명을 전달:

```
X-OS-App-Cred-Id:     <application-credential-id>
X-OS-App-Cred-Secret: <application-credential-secret>
X-OS-Auth-Url:        https://keystone.example.com:5000/v3   # 선택적 재정의
```

#### 컨테이너 (Containerfile 제공)

```bash
podman build -t openstack-mcp .
podman run --rm -p 8001:8001 \
  --env-file config.env \
  -v /var/log/kolla:/var/log/kolla:ro \
  openstack-mcp
```

---

## 설정

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MCP_PORT` | `8001` | HTTP 수신 포트 |
| `OS_AUTH_URL` | `http://127.0.0.1:5000/v3` | Keystone 엔드포인트 (서버 기본값; 호출자별 재정의 가능) |
| `OSMCP_DOMAINS` | 전체 | 쉼표 구분 부분집합: `compute,network,lbaas,storage,image,identity,observability` |
| `OSMCP_TIERS` | 전체 | 쉼표 구분 부분집합: `read,write,maintain` |
| `KOLLA_LOG_DIR` | `/var/log/kolla` | Kolla 서비스 로그 디렉터리 루트 |
| `MCP_NODE_NAME` | 호스트명 | 어느 노드의 로그를 서빙 중인지 식별하는 라벨 |
| `MCP_ALLOWED_HOST_NAMES` | `localhost,127.0.0.1` | 쉼표 구분 호스트명; 각 항목에 `:<MCP_PORT>`가 붙어 Host 헤더 허용 목록을 구성. 전체 목록을 직접 지정하려면 `MCP_ALLOWED_HOSTS`를 설정 |

---

## 툴 레퍼런스

### 코어 (모든 도메인)

| 툴 | 설명 |
|---|---|
| `whoami` | 자격증명 유무와 현재 프로젝트/역할 표시 |

### 컴퓨트 (Nova)

| 툴 | 설명 |
|---|---|
| `server_list` | 인스턴스 목록 (키 컬럼: id, name, status) |
| `server_show` | id로 인스턴스 하나 조회 |
| `server_update` | 인스턴스 이름/설명 수정 |
| `server_delete` | 인스턴스 삭제 (사람 확인 필요) |
| `server_start` | SHUTOFF 인스턴스 전원 켜기 |
| `server_stop` | ACTIVE 인스턴스 전원 끄기 |
| `flavor_list` | 컴퓨트 플레이버 목록 |
| `flavor_show` | 플레이버 하나 조회 |
| `flavor_delete` | 플레이버 삭제 |
| `keypair_list` | 키페어 목록 |
| `keypair_delete` | 키페어 삭제 |
| `hypervisor_list` | 컴퓨트 하이퍼바이저 목록 |
| `availability_zone_list` | 가용 영역(AZ) 목록 |
| `aggregate_list` | 호스트 애그리게이트 목록 (관리자) |
| `aggregate_show` | 호스트 애그리게이트 하나 조회 |
| `aggregate_update` | 애그리게이트 이름 수정 |
| `aggregate_delete` | 호스트 애그리게이트 삭제 |
| `server_group_list` | 서버 그룹 목록 |
| `server_group_show` | 서버 그룹 하나 조회 |
| `server_group_delete` | 서버 그룹 삭제 |
| `quota_show` | 프로젝트의 컴퓨트/네트워크/스토리지 쿼터 + 사용량 |
| `capacity_stats` | vCPU/RAM/디스크 총 용량 대비 사용량 집계 (Placement) |

### 네트워크 (Neutron)

| 툴 | 설명 |
|---|---|
| `network_list` | 네트워크 목록 |
| `network_show` | 네트워크 하나 조회 |
| `network_update` | 네트워크 이름/설명 수정 |
| `network_delete` | 네트워크 삭제 |
| `subnet_list` | 서브넷 목록 |
| `subnet_show` | 서브넷 하나 조회 |
| `subnet_update` | 서브넷 수정 |
| `subnet_delete` | 서브넷 삭제 |
| `router_list` | 라우터 목록 |
| `router_show` | 라우터 하나 조회 |
| `router_update` | 라우터 수정 |
| `router_delete` | 라우터 삭제 |
| `port_list` | 포트 목록 |
| `port_show` | 포트 하나 조회 |
| `port_update` | 포트 수정 |
| `port_delete` | 포트 삭제 |
| `security_group_list` | 보안 그룹 목록 |
| `security_group_show` | 보안 그룹 하나 조회 |
| `security_group_update` | 보안 그룹 수정 |
| `security_group_delete` | 보안 그룹 삭제 |
| `security_group_rule_list` | 보안 그룹 규칙 목록 |
| `security_group_rule_delete` | 보안 그룹 규칙 삭제 |
| `floating_ip_list` | 플로팅 IP 목록 |
| `floating_ip_show` | 플로팅 IP 하나 조회 |
| `floating_ip_update` | 플로팅 IP 수정 |
| `floating_ip_delete` | 플로팅 IP 반환 |
| `agent_list` | Neutron 에이전트 목록 (관리자) |
| `agent_show` | 에이전트 하나 조회 |
| `rbac_policy_list` | RBAC 정책 목록 |
| `rbac_policy_show` | RBAC 정책 하나 조회 |
| `network_ip_availability_list` | 네트워크별 IP 가용량 (관리자) |
| `network_ip_availability_show` | 네트워크 하나의 IP 가용량 조회 |

### LBaaS (Octavia)

| 툴 | 설명 |
|---|---|
| `load_balancer_list` | 로드 밸런서 목록 |
| `load_balancer_show` | 로드 밸런서 하나 조회 |
| `load_balancer_update` | 로드 밸런서 수정 |
| `load_balancer_delete` | 로드 밸런서 삭제 |
| `listener_list` | 리스너 목록 |
| `listener_show` | 리스너 하나 조회 |
| `listener_update` | 리스너 수정 |
| `listener_delete` | 리스너 삭제 |
| `pool_list` | 풀 목록 |
| `pool_show` | 풀 하나 조회 |
| `pool_update` | 풀 수정 |
| `pool_delete` | 풀 삭제 |
| `health_monitor_list` | 헬스 모니터 목록 |
| `health_monitor_show` | 헬스 모니터 하나 조회 |
| `health_monitor_update` | 헬스 모니터 수정 |
| `health_monitor_delete` | 헬스 모니터 삭제 |
| `l7_policy_list` | L7 정책 목록 |
| `l7_policy_show` | L7 정책 하나 조회 |
| `l7_policy_delete` | L7 정책 삭제 |
| `lb_flavor_list` | LBaaS 플레이버 목록 |
| `lb_flavor_show` | LBaaS 플레이버 하나 조회 |

### 스토리지 (Cinder)

| 툴 | 설명 |
|---|---|
| `volume_list` | 블록 볼륨 목록 |
| `volume_show` | 볼륨 하나 조회 |
| `volume_update` | 볼륨 이름/설명 수정 |
| `volume_delete` | 볼륨 삭제 |
| `volume_snapshot_list` | 스냅샷 목록 |
| `volume_snapshot_show` | 스냅샷 하나 조회 |
| `volume_snapshot_delete` | 스냅샷 삭제 |
| `volume_type_list` | 볼륨 타입 목록 |
| `volume_backup_list` | 백업 목록 |
| `volume_backup_show` | 백업 하나 조회 |
| `volume_backup_delete` | 백업 삭제 |
| `volume_group_list` | 볼륨 그룹 목록 |
| `volume_group_show` | 그룹 하나 조회 |
| `volume_group_type_list` | 그룹 타입 목록 |
| `volume_group_type_show` | 그룹 타입 하나 조회 |
| `volume_group_snapshot_list` | 그룹 스냅샷 목록 |
| `volume_group_snapshot_show` | 그룹 스냅샷 하나 조회 |
| `volume_service_list` | Cinder 백엔드 서비스 목록 (관리자) |

### 이미지 (Glance)

| 툴 | 설명 |
|---|---|
| `image_list` | 이미지 목록 |
| `image_show` | 이미지 하나 조회 |
| `image_delete` | 이미지 삭제 |
| `metadef_namespace_list` | 메타데이터 정의 네임스페이스 목록 |
| `metadef_namespace_show` | 네임스페이스 하나 조회 |

### 아이덴티티 (Keystone)

| 툴 | 설명 |
|---|---|
| `project_list` | 프로젝트 목록 |
| `project_show` | 프로젝트 하나 조회 |
| `project_delete` | 프로젝트 삭제 |
| `domain_list` | 도메인 목록 |
| `domain_show` | 도메인 하나 조회 |
| `domain_delete` | 도메인 삭제 |
| `user_list` | 사용자 목록 |
| `user_show` | 사용자 하나 조회 |
| `user_update` | 사용자 이름/이메일 수정 |
| `user_delete` | 사용자 삭제 |
| `role_list` | 역할 목록 |
| `role_delete` | 역할 삭제 |
| `role_assignment_list` | 역할 할당 목록 |
| `application_credential_list` | 앱 크리덴셜 목록 (현재 사용자) |
| `region_list` | 리전 목록 |
| `region_show` | 리전 하나 조회 |
| `service_list` | 카탈로그 서비스 목록 (관리자) |
| `service_show` | 카탈로그 서비스 하나 조회 |
| `endpoint_list` | 카탈로그 엔드포인트 목록 (관리자) |
| `endpoint_show` | 엔드포인트 하나 조회 |

### 관측성 (Observability)

| 툴 | 설명 |
|---|---|
| `log_targets` | 사용 가능한 Kolla 로그 타깃(서비스별 디렉터리) 목록 |
| `log_tail` | 타깃 로그 하나를 시간창 + grep 필터링으로 tail |
| `log_trace` | OpenStack 요청 ID(`req-...`)로 서비스 간 추적 |
| `service_status` | Nova 컴퓨트 서비스 + Neutron 에이전트 상태(up/down) |

---

## 확장하기: 새 리소스 추가

Create 툴은 의도적으로 구현하지 않았으며, 이는 주요 확장 지점입니다. create 툴이나 새 리소스 타입을 추가하려면:

1. openstacksdk를 사용해 `src/server.py` 또는 `src/os_backend.py`에 함수를 추가합니다.
2. `add(fn, name="...", domain="...", tier="write")`로 등록합니다.
3. 완전한 CRUD 리소스라면 `RESOURCES`에 딕셔너리 하나와 `RESOURCE_DOMAIN` 매핑 항목을 추가하면, `_make_list/_make_show/_make_update/_make_delete`가 툴을 자동 생성합니다.

openstacksdk가 지원하는 어떤 OpenStack 서비스든 이런 식으로 몇 줄만에 연결할 수 있습니다.

---

## 테스트 실행

```bash
pytest -q
```

스모크 스위트가 검증하는 것: 툴 레지스트리가 비어 있지 않고 예상되는 OpenStack 툴을 포함(그리고 비-OpenStack 툴은 제외)하는지, 레거시 router 모듈이 없는지, Kolla 로그 백엔드가 타깃 해석 및 요청 ID 파싱을 올바르게 수행하는지.

---

## 라이선스

MIT — [LICENSE](LICENSE) 참조.

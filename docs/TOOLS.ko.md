# 툴 레퍼런스

[English](TOOLS.md) | **한국어** · [← README](../README.ko.md)

7개 도메인에 걸친 123개 툴. `*_delete` 툴은 MCP elicitation을 통해 사람의 명시적 확인을 요구합니다. 서버 실행 방법은 [USAGE.ko.md](USAGE.ko.md)를 참고하세요.

---

## 코어 (모든 도메인)

| 툴 | 설명 |
|---|---|
| `whoami` | 자격증명 유무와 현재 프로젝트/역할 표시 |

## 컴퓨트 (Nova)

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

## 네트워크 (Neutron)

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

## LBaaS (Octavia)

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

## 스토리지 (Cinder)

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

## 이미지 (Glance)

| 툴 | 설명 |
|---|---|
| `image_list` | 이미지 목록 |
| `image_show` | 이미지 하나 조회 |
| `image_delete` | 이미지 삭제 |
| `metadef_namespace_list` | 메타데이터 정의 네임스페이스 목록 |
| `metadef_namespace_show` | 네임스페이스 하나 조회 |

## 아이덴티티 (Keystone)

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

## 관측성 (Observability)

| 툴 | 설명 |
|---|---|
| `log_targets` | 사용 가능한 Kolla 로그 타깃(서비스별 디렉터리) 목록 |
| `log_tail` | 타깃 로그 하나를 시간창 + grep 필터링으로 tail |
| `log_trace` | OpenStack 요청 ID(`req-...`)로 서비스 간 추적 |
| `service_status` | Nova 컴퓨트 서비스 + Neutron 에이전트 상태(up/down) |

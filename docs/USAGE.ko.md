# 사용법

[English](USAGE.md) | **한국어** · [← README](../README.ko.md)

OpenStack-MCP를 stdio 및 HTTP 모드로 설치하고 실행하는 방법입니다. 전체 툴 목록은 [TOOLS.ko.md](TOOLS.ko.md)를 참고하세요.

---

## 사전 요구사항

- Python 3.10 이상
- Keystone **애플리케이션 크리덴셜**이 있는 OpenStack 클라우드 (수행할 작업에 따라 프로젝트 또는 도메인 스코프)
- Kolla 로그 툴을 쓰려면 서버 호스트에서 `/var/log/kolla`에 접근 가능해야 함 (또는 컨테이너에 마운트)

## 설치

```bash
git clone https://github.com/YeeDochi/OpenStack-MCP.git
cd OpenStack-MCP
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## stdio 모드 (Claude Desktop / `claude mcp add`)

환경변수를 설정한 뒤 실행:

```bash
export OS_AUTH_URL=https://keystone.example.com:5000/v3
export OS_APPLICATION_CREDENTIAL_ID=<your-app-cred-id>
export OS_APPLICATION_CREDENTIAL_SECRET=<your-app-cred-secret>

PYTHONPATH=src python -m core.server --transport stdio
```

Claude Desktop 설정 (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "openstack": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "core.server", "--transport", "stdio"],
      "cwd": "/path/to/OpenStack-MCP",
      "env": {
        "PYTHONPATH": "/path/to/OpenStack-MCP/src",
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
claude mcp add --transport stdio openstack --env PYTHONPATH=/path/to/OpenStack-MCP/src -- /path/to/.venv/bin/python -m core.server
```

---

## HTTP 모드 (다중 사용자 / 컨테이너)

```bash
# 예시 설정 파일을 복사해 편집
cp config.env.example config.env
# OS_AUTH_URL, MCP_PORT, KOLLA_LOG_DIR 등을 설정

PYTHONPATH=src python -m core.server --transport http --host 0.0.0.0 --port 8001
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

### 컨테이너 (Containerfile 제공)

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
| `MCP_DOMAINS` | 전체 | 쉼표 구분 부분집합: `compute,network,lbaas,storage,image,identity,observability` |
| `MCP_TIERS` | 전체 | 쉼표 구분 부분집합: `read,write,maintain` |
| `KOLLA_LOG_DIR` | `/var/log/kolla` | Kolla 서비스 로그 디렉터리 루트 |
| `MCP_NODE_NAME` | 호스트명 | 어느 노드의 로그를 서빙 중인지 식별하는 라벨 |
| `MCP_ALLOWED_HOST_NAMES` | `localhost,127.0.0.1` | 쉼표 구분 호스트명; 각 항목에 `:<MCP_PORT>`가 붙어 Host 헤더 허용 목록을 구성. 전체 목록을 직접 지정하려면 `MCP_ALLOWED_HOSTS`를 설정 |

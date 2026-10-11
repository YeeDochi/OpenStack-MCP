---
name: openstack-profile
description: Switch between OpenStack MCP connection profiles (different clouds/accounts) and manage them. Use when the user wants to change which OpenStack MCP server or account Claude Code is connected to, or to add/list/remove connection profiles. Triggers - "switch profile", "change cloud/server", "add a profile", "/openstack-profile".
---

# OpenStack 연결 프로필 관리

이 스킬은 클라이언트의 MCP 등록(`openstack-*`)을 **프로필** 단위로 전환/관리한다.
프로필 하나 = 하나의 클라우드(메인 MCP `BASE_URL`) + 계정(creds).

## 백엔드 스크립트 선택 (OS별)
- Windows → `<skill-dir>/openstack-profile.ps1`. 실행기는 **버전 무관하게 있는 걸 자동 선택** —
  `pwsh`(PowerShell 7)가 있으면 그걸, 없으면 윈도우 기본 내장 `powershell`(5.1)을 쓴다.
  한 줄로: `$ps = if (Get-Command pwsh -EA SilentlyContinue) { 'pwsh' } else { 'powershell' }`.
  (`pwsh` 하나로 박으면 PS7 미설치 PC에서 `CommandNotFoundException` 나니 박지 말 것.)
- macOS/Linux → `<skill-dir>/openstack-profile.sh`를 `bash`로 실행.
`<skill-dir>` = 이 SKILL.md가 있는 디렉터리(보통 `~/.claude/skills/openstack-profile`).

## 동작
- **목록**: `bash <skill-dir>/openstack-profile.sh list`
  (Win: `$ps = if (Get-Command pwsh -EA SilentlyContinue) { 'pwsh' } else { 'powershell' }; & $ps -ExecutionPolicy Bypass -File <skill-dir>\openstack-profile.ps1 list`). 활성 프로필은 `* (active)`로 표시.
- **추가** `add <name>`: 실행하면 **프롬프트로 값을 받는다**. ⚠️ Claude Code의 `!` 실행은 TTY가 없어 대화형 프롬프트(`read`)가 건너뛰어져 `BASE_URL required`로 죽는다. 그러니 **사용자에게 "별도 터미널(실제 셸)을 열어 직접 실행"하라고 안내**한다 — 그래야 프롬프트가 뜨고, 비밀도 모델 컨텍스트를 안 거친다(인자로 넘기지 말 것). `!` 안에서 꼭 해야 하면 아래 하이브리드(env 프리필)나 `OPENSTACK_PROFILE_NONINTERACTIVE=1`을 쓰되, 그 경우 비밀이 명령에 노출된다.
  - 필수(비면 재요청): `BASE_URL`, `OS_AUTH_URL`, `OS_APP_CRED_ID`, `OS_APP_CRED_SECRET`(가려진 입력).
  - `DOMAINS`(등록할 서비스 마운트 서브셋)는 **프롬프트하지 않음** — 기본값=전체(compute network storage lbaas image identity observability). 서브셋이 필요할 때만 env로: `DOMAINS="compute network" bash ...sh add <name>`. (`switch`는 프로필에 DOMAINS 줄이 있으면 그대로 존중.)
  - 비대화형 호출(설치 스크립트 등)은 `OPENSTACK_PROFILE_NONINTERACTIVE=1`로 모든 프롬프트를 끔 — env로 넘긴 값만 저장.
  - Unix(**별도 터미널에서**): `bash <skill-dir>/openstack-profile.sh add <name>`
  - Win(**별도 터미널에서**): `$ps = if (Get-Command pwsh -EA SilentlyContinue) { 'pwsh' } else { 'powershell' }; & $ps -ExecutionPolicy Bypass -File <skill-dir>\openstack-profile.ps1 add <name>`
  - **하이브리드**: 환경변수를 미리 세팅한 필드는 프롬프트를 건너뛴다(자동화용). 예) `BASE_URL=... OS_AUTH_URL=... OS_APP_CRED_ID=... OS_APP_CRED_SECRET=... bash <skill-dir>/openstack-profile.sh add <name>`.
  - **기존 기반 추가**: `FROM=<기존> bash <skill-dir>/openstack-profile.sh add <new>` — 상속된 값은 프롬프트에서 자동 생략, 바꿀 것만 입력.
- **전환** `switch <name>`: `bash <skill-dir>/openstack-profile.sh switch <name>`. 끝나면 사용자에게
  **`claude --continue`(최근 대화 이어서) 또는 `claude --resume`(세션 선택)로 재시작** 하라고 안내 —
  대화 유지하며 새 MCP 등록만 적용(그냥 재시작하면 대화가 사라짐). "지금 어느 프로필/프로젝트냐"는 `whoami`로도 확인.
- **삭제** `remove <name>`.

## 주의
- 프로필은 평문 creds → 사용자 홈(`$OPENSTACK_PROFILE_DIR` 또는 OS 기본)에만. 비밀번호/시크릿 화면 출력 금지.
- 프로필은 OS-로컬 — Windows와 Unix 간 복사해 쓰지 말 것(생성한 OS에서만 사용).

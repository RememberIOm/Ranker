# Ranker — CLAUDE.md

## 프로젝트 개요

세션 기반 Elo 평점 시스템 웹앱. 1대1 배틀 투표로 항목을 실시간 랭킹화한다.
인증 없이 쿠키 기반 세션 ID로 사용자별 JSON 파일을 분리 관리한다.

- 개인 취미 프로젝트 — **최신 패키지·기술 스택, 코딩 모범 사례를 적극적으로 도입한다.**
- **문서화 원칙**: 코드 변경 시 CLAUDE.md를 항상 함께 갱신한다. 구조·아키텍처·스택이 바뀌면 즉시 반영한다.
- 배포: https://battle-ranker.fly.dev (Fly.io, 도쿄 리전)

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.13 |
| 웹 프레임워크 | FastAPI ≥ 0.135 |
| 서버 | uvicorn[standard] ≥ 0.42 |
| 템플릿 | Jinja2 v3 |
| 파일 I/O | aiofiles (전부 async) |
| 폼 파싱 | python-multipart |
| 패키지 관리 | uv |
| CSS | TailwindCSS v4 (CLI 사전 빌드) |
| 차트 | Chart.js (CDN) |
| 컨테이너 | Docker (multi-stage: node:22-alpine → python:3.13-slim) |
| 호스팅 | Fly.io |
| CI/CD | GitHub Actions → flyctl deploy (SHA 핀) |
| Dev 도구 | ruff, pytest, pytest-asyncio, httpx, pip-audit |

---

## 개발 환경

```bash
# 개발 서버 실행 (Docker 격리, hot reload 포함)
# app 서비스(uvicorn) + tailwind 서비스(watch 모드)가 동시 시작됨
docker compose up

# 의존성 추가 후 lockfile 갱신
uv add <패키지명>   # pyproject.toml + uv.lock 자동 갱신

# 세션 데이터 저장 경로
# 로컬(docker compose): ranker_data 볼륨 → /data/sessions/
# Fly.io: ranker_data 볼륨 → /data/sessions/

# Tailwind 클래스 변경 시
# → tailwind 서비스가 자동으로 static/output.css 재빌드 (watch 모드)
# → 새 클래스 추가 후 브라우저 새로고침으로 확인

# 테스트 실행
uv run python -m unittest discover -s tests -p "test_*.py"

# dev 의존성 설치 (ruff, pytest, httpx, pip-audit 등)
uv sync --extra dev

# 의존성 보안 감사
uv run pip-audit

# Fly 시크릿 관리
flyctl secrets set KEY=value

# 세션 데이터 백업 (Fly SSH)
flyctl ssh console
tar czf /tmp/sessions.tar.gz /data/sessions
```

---

## 프로젝트 구조

```
main.py          # FastAPI 앱 + 세션 쿠키 라우트 (/, /start, /upload, /end-session)
deps.py          # 의존성 주입 (세션 검증, DataStore 주입)
store.py         # 세션별 JSON 저장소 + 인메모리 캐시 + TTL 정리
schemas.py       # Pydantic 응답 스키마
services.py      # 순수 비즈니스 로직 (Elo, 매치메이킹, 정규화)
entrypoint.sh    # Docker 엔트리포인트 — Fly 볼륨 소유권 보정 + 권한 강하
input.css        # Tailwind CSS v4 소스 (빌드 → static/output.css)
routers/
  battle.py      # /battle — 배틀 화면 & 투표 API
  ranking.py     # /ranking — 랭킹 보드
  manage.py      # /manage — 항목·기준·설정 CRUD + 데이터 I/O
templates/
  base.html         # 레이아웃, 다크모드, 네비게이션, 공유 유틸(토스트·confirm·fetchWithTimeout)
  index.html        # 홈 / 세션 업로드 페이지
  battle.html       # 1대1 다중 기준 투표 UI
  battle_empty.html # 배틀 빈 상태 (항목/기준 부족 시)
  ranking.html      # 랭킹 테이블 + Chart.js
  manage.html       # 관리 인터페이스
static/
  output.css     # Tailwind 빌드 아티팩트 (.gitignore 처리)
```

---

## 아키텍처 핵심

### 세션 스토리지
- 쿠키 `session_id` (httponly, max_age 7일) → `{SESSION_DIR}/{session_id}.json`
- `SessionCookieRefreshMiddleware`: 모든 응답에서 유효한 세션 쿠키를 갱신 — 활성 사용자의 TTL 자동 연장
- `DataStore`: 파일을 인메모리에 캐시, 변경 시 `aiofiles`로 비동기 저장
- 세션별 `asyncio.Lock`으로 동시 쓰기 보호
- 7일 경과 세션 자동 삭제 (`cleanup_expired_sessions`)
- 캐시 미스(서버 재시작) 시 파일 mtime 자동 touch → cleanup이 활성 세션 오삭제 방지
- **데이터 보호 정책**: `InvalidSessionDataError` 발생 시 세션 파일을 절대 삭제하지 않음 — 파일 손상이 의심돼도 사용자가 재업로드로 복구할 수 있도록 보존
- **`active_round` 영속화**: 진행 중인 배틀 라운드 토큰을 JSON에 저장 → Fly.io 자동 스케일다운으로 VM 재시작돼도 사용자가 이어서 투표 가능. `delete_item`/`set_criteria`/`import_json` 등 항목 변동 시 invalidate됨
- **`SessionSaveError`**: `_save_locked`의 OS 계열 예외를 감싸 로깅 후 라우터에서 사용자 친화 500 응답으로 매핑
- **구조적 로깅**: `ranker` 네임스페이스 (`ranker.store`, `ranker.battle`, `ranker.manage`, `ranker.lifespan`) — 모든 핵심 경로에 `logger.info/warning/error` 적용

### Elo 알고리즘 (services.py)
- **다이나믹 K-Factor**: 초반 100 → 대전 수 증가 → 최소 30으로 수렴
- **드로우 확률**: 기본값은 점수 차이 기반 가우시안 곡선 (draw_max=0.33). 기준별 실전 20+ 배틀 데이터 누적 시 실측 무승부 비율로 자동 보정
- **기준별 통계**: `CriterionModel.battles` / `CriterionModel.draws` — 투표마다 누적, 기준 편집 시 동일 key의 이력 보존
- **인플레이션 억제**: 배틀 후 백그라운드 태스크로 평균 회귀 (목표 1200). 서버 재시작 후 첫 번째 투표에서 즉시 정규화 실행
- **다중 기준 동시 반영**: 한 번의 투표로 모든 기준 Elo 갱신

### 매치메이킹 (Power of Two Choices)
- **item1**: 무작위 2개 샘플 중 `matches_played` 적은 쪽 선택 → 탐험 촉진
- **item2**: item1 제외 무작위 2개 샘플 중 **가중 복합 점수** 차이 작은 쪽 선택 → 공정 매칭
- `services.composite_rating()` 헬퍼로 매치메이킹·랭킹·순위 계산에서 동일 로직 사용
- 별도 설정값 없음 — `match_smart_rate`, `match_score_range` 제거됨

### 가중 복합 점수
- `services.composite_rating()`: 기준별 weight를 곱해 가중 합산 — 단일 진실 소스
- `/ranking`, `get_item_rank`, 매치메이킹에서 모두 동일 헬퍼 사용
- 라운딩은 표시 계층(ranking.html 렌더 시점)에서만 수행

---

## 코딩 가이드라인

### 언어 및 스타일
- **Python**: `match-case` 적극 활용, 타입 힌트 필수(`X | None` 표기, `Optional` 지양), `async/await` 일관 사용
- **파일 I/O**: 반드시 `aiofiles` 사용, 동기 파일 I/O 금지
- **의존성**: 최신 안정 버전 유지, 과감하게 업그레이드 — 릴리스 노트 확인 후 적용
- **FastAPI**: 경로 함수는 `async def`, `Depends()` 패턴 유지, Pydantic v2 모델 사용
- **Jinja2**: 로직은 Python 라우터에서, 템플릿은 표시만 담당
- **CSS**: TailwindCSS v4 유틸리티 클래스만 사용, 별도 CSS 파일 최소화
- DB 도입 계획 없음 — JSON 스토리지 구조 유지

### 모범 사례 (Best Practices)
- **최신 문법 우선**: Python 신규 구문(PEP 695 타입 별칭, PEP 696 기본 타입 파라미터 등)을 적극 활용한다
- **불필요한 추상화 금지**: 단일 사용 헬퍼·유틸 함수 남발 금지, 실제 필요 복잡도만 유지
- **보안**: 사용자 입력은 경계에서만 검증, SQL·XSS·커맨드 인젝션 방지 기본 원칙 준수
- **에러 처리**: 발생 불가능한 시나리오에 대한 방어 코드 추가 금지, 실제 경계(외부 입력, 파일 시스템)만 처리
- **비동기 일관성**: `asyncio` 태스크·락 사용 시 취소 안전성(cancellation safety) 고려
- **코드 리뷰 기준**: 타입 힌트 누락, 동기 I/O 혼용, 불필요한 `pass`/`...` 패턴 지적

### 문서 갱신 규칙
- 구조·라우터·스키마 변경 → **프로젝트 구조** 섹션 즉시 갱신
- 기술 스택 추가/변경/제거 → **기술 스택** 표 즉시 갱신
- 아키텍처 패턴 변경 → **아키텍처 핵심** 섹션 즉시 갱신
- 배포 설정 변경 → **배포** 섹션 즉시 갱신

---

## 배포

```bash
# Fly.io 수동 배포
flyctl deploy --remote-only

# main 브랜치 push 시 GitHub Actions가 자동 배포
```

- VM: 1 CPU, 512MB RAM, min 0 머신 (유휴 시 스케일다운)
- 볼륨 `ranker_data` → `/data` (세션 데이터 영속)
- `entrypoint.sh`: 컨테이너 시작 시 Fly 볼륨 소유권을 appuser로 보정 후 권한 강하 — 볼륨 마운트가 빌드 시 `chown`을 덮어씌우는 문제 방지
- HTTPS 강제, HTTP/2 지원

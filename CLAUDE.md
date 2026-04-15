# Ranker — CLAUDE.md

## 프로젝트 개요

세션 기반 Bayesian BT 평점 시스템 웹앱. 1대1 또는 3-way 배틀 투표로 항목을 실시간 랭킹화한다.
인증 없이 쿠키 기반 세션 ID로 사용자별 데이터를 단일 SQLite DB에서 관리한다.

- 개인 취미 프로젝트 — **새 코드 작성 시 최신 문법·모범 사례를 적용한다.** 기존 코드의 스타일 변경은 요청 시에만 수행한다. 의존성은 최신 안정 버전을 유지한다.
- 배포: https://battle-ranker.fly.dev (Fly.io, 도쿄 리전)

---

## 개발 환경

```bash
docker compose up          # 개발 서버 (uvicorn hot-reload + tailwind watch)
uv run pytest              # 테스트 실행
uv sync --extra dev        # dev 의존성 설치
```

---

## 아키텍처 핵심

### 설계 정책
- **데이터 보호**: `InvalidSessionDataError` 발생 시 세션 데이터를 절대 삭제하지 않음 — 사용자가 재업로드로 복구할 수 있도록 보존
- **`active_round` 영속화**: 배틀 라운드 토큰을 DB에 저장 → Fly.io 자동 스케일다운으로 VM 재시작돼도 투표 이어서 가능. `delete_item`/`set_criteria`/`import_json` 시 invalidate

### SQLite 스토리지 (database.py + store.py)
- **단일 DB 파일**: `/data/ranker.db` (`DATABASE_PATH` 환경 변수로 오버라이드)
- **WAL 모드**: 읽기 동시성 보장, 단일 워커 전제
- **스키마**: `sessions`, `criteria`, `items`, `item_ratings`, `active_rounds` — `ON DELETE CASCADE`로 세션 삭제 시 관련 데이터 자동 정리
- **In-Memory 패턴**: `DataStore.create()` 시 전체 세션 데이터를 메모리에 로드, 변경 후 단일 트랜잭션으로 DB에 기록. `services.py`의 in-place 변이 패턴과 호환
- **동시성**: 세션별 `asyncio.Lock`으로 load-mutate-save 사이클 보호 (단일 uvicorn 워커 전제)
- **JSON 마이그레이션**: 앱 시작 시 `SESSION_DIR`에 남은 JSON 파일을 자동 마이그레이션 → `migrated/` 폴더로 이동

### Bayesian Bradley-Terry 알고리즘 (services.py)
- **Online Laplace Approximation**: 항목별·기준별 사후분포 `(μ, σ²)` 유지 — μ는 실력 추정, σ²는 불확실성
- **업데이트 공식**: `p = sigmoid(μ_a - μ_b)`, Fisher 정보 `w = p(1-p)`, gradient `g = outcome - p`. 평균: `μ_new = μ + g/(τ+w)`, 정밀도: `τ_new = τ + w`
- **σ² 하한**: 0.01 (과도한 확신 방지)
- **드로우 확률**: logit 스케일 가우시안 감쇠 + Bayesian Beta prior로 실측 무승부 비율에 자연 수렴
- **계층적 축소**: 투표 후 기준 간 **Leave-One-Out(LOO) 정밀도 가중 평균** 방향으로 축소. 적응형 강도: `base_strength / (1 + criterion_matches)`. σ² 미변경. `hierarchical_strength=0`이면 비활성
- **정규화 불필요**: Bayesian prior가 인플레이션 방지 역할을 대체 — 별도 정규화 추가 금지
- **표시 변환**: `μ × display_scale + display_center` (기본 173.72/1200)

### 3-way 배틀 모드
- Best>Middle, Best>Worst, Middle>Worst 3개 쌍대비교로 분해
- **동시 업데이트**: 원본 값에서 모든 그래디언트·Fisher 정보를 계산 후 일괄 적용 — 순차 적용 시 업데이트 순서 편향 발생하므로 반드시 유지
- **Tied 모드**: Best만 선택 시 나머지 둘은 outcome=0.5 (무승부)
- **통계**: 3-way 1회 = 3 battles, 각 항목 2 criterion_matches 증가, Tied 시 draws 1 증가
- 항목 3개 미만 시 자동으로 2-way 전환

### HTMX partial 응답 (routers/*.py + templates/partials/)
- **self-hosted HTMX 2.0.4**: `/static/vendor/htmx.min.js`
- **`is_htmx(request)`** (`deps.py`): `HX-Request: true` 헤더 감지. 모든 라우터 핸들러에서 HTMX/일반 요청 분기에 사용
- **partial 템플릿**: `templates/partials/` 디렉토리. 배틀 카드, 결과 모달, 관리 항목 목록 등
- **배틀 투표 흐름**: 클라이언트 JS가 `fetch()` + `HX-Request: true` 헤더로 JSON body 전송 → 서버가 결과 모달 HTML + OOB swap으로 다음 배틀 카드 반환 → JS가 DOM에 적용
- **OOB swap**: 투표 응답에 `<div id="battle-arena" hx-swap-oob="innerHTML">` 포함하여 다음 배틀 카드를 프리로드
- **Graceful degradation**: HTMX가 아닌 요청은 기존 JSON/redirect 응답 유지. 모든 form에 `action`+`method` 유지

### 가중 복합 점수
- `services.composite_rating()`: 기준별 display_rating × weight 가중 합산 — **단일 진실 소스**
- 라운딩은 표시 계층(ranking.html 렌더 시점)에서만 수행

---

## 코딩 가이드라인

- **타입 힌트 필수**: 함수 파라미터 + 반환 타입 모두 표기. `X | None` 사용, `Optional` 지양
- **최신 문법**: union type, match-case 등 Python·라이브러리의 최신 안정 문법 사용
- **DB I/O**: `aiosqlite`로 비동기 SQLite 접근, `database.get_db()` 싱글턴 커넥션 사용
- **FastAPI**: 경로 함수는 `async def`, `Depends()` 패턴 유지, Pydantic v2 모델 사용
- **Jinja2**: 로직은 Python 라우터에서, 템플릿은 표시만 담당
- **불필요한 추상화 금지**: 단일 사용 헬퍼·유틸 함수 남발 금지
- **에러 처리**: 발생 불가능한 시나리오에 대한 방어 코드 추가 금지, 실제 경계(외부 입력, 파일 시스템)만 처리
- **비동기 일관성**: `asyncio` 태스크·락 사용 시 취소 안전성(cancellation safety) 고려
- **로깅**: `ranker.*` 네임스페이스 사용 (`ranker.store`, `ranker.battle` 등)

### 테스트
- **새 기능**: 핵심 로직에 대한 테스트를 함께 작성 (서비스 유닛 or 라우터 통합)
- **버그 수정**: 회귀 테스트 추가
- **프레임워크**: pytest + pytest-asyncio (`asyncio_mode = "auto"`)
- **fixture 패턴**: `tempfile.NamedTemporaryFile` + `database.DB_PATH` 교체 → `database.init_db()` (기존 테스트 참조)
- **범위**: 알고리즘 정확성, 데이터 무결성, 입력 검증에 집중 — UI 렌더링 테스트 제외

### 커밋 메시지
- **형식**: `<Type>: <한국어 요약>` (예: `Fix: 3-way 배틀 렌더링 오류 수정`)
- **Type**: `Fix` (버그), `Improve` (개선·리팩터링), `Feat` (새 기능), `Chore` (빌드·설정), `Docs` (문서)

### 한국어/영어 규칙
- **코드 식별자**: 영어 / **주석·독스트링**: 한국어
- **사용자 대면 문자열**: 한국어 / **로그 메시지**: 영어 snake_case (`"stale_round"`)

### HTMX
- **HTMX 속성**: `hx-post`, `hx-target`, `hx-swap`, `hx-confirm`, `hx-swap-oob` 사용
- **토스트 피드백**: `HX-Trigger` 응답 헤더의 `showToast` 이벤트를 `base.html`의 리스너가 처리
- **에러 처리**: `htmx:responseError` 글로벌 이벤트 → `showToast()`로 표시
- **확인 다이얼로그**: `hx-confirm` → `htmx:confirm` 이벤트 → `showConfirm()` 커스텀 다이얼로그

### 프론트엔드 변경 시
- `docker compose up`으로 브라우저에서 직접 확인: 정상 경로, 빈 상태, 다크모드, 모바일 뷰포트
- HTMX partial swap 후 스타일·상태 초기화 확인

### 문서 갱신 규칙
- 아키텍처 패턴 변경 → **아키텍처 핵심** 섹션 갱신
- 코딩 규칙·컨벤션 변경 → **코딩 가이드라인** 섹션 갱신

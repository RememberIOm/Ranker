# Ranker (Session-based Bayesian Bradley-Terry Rating System)

**Ranker**는 1:1 대결 투표를 통해 실시간으로 순위를 산정하는 범용 랭킹 웹 애플리케이션입니다.
영화, 음식, 게임, 애니메이션 등 **어떤 주제든** 사용자가 원하는 항목과 평가 기준을 자유롭게 설정하여 나만의 랭킹을 만들 수 있습니다.

**Online Bayesian Bradley-Terry** 모델을 채택하여 각 항목의 실력과 불확실성(신뢰 구간)을 동시에 추정하고, 계층적 축소(Hierarchical Shrinkage)로 기준 간 정보를 공유합니다.

단일 데이터베이스 대신 **사용자별 세션 기반 JSON 저장소**를 채택하여, 누구나 독립적인 환경에서 자신만의 데이터를 구축하고 백업(Export/Import)할 수 있습니다.

![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat-square&logo=fastapi)
![TailwindCSS](https://img.shields.io/badge/TailwindCSS-v4-38B2AC?style=flat-square&logo=tailwind-css)
![Fly.io](https://img.shields.io/badge/Deployed_on-Fly.io-7b51b6?style=flat-square&logo=fly.io&logoColor=white)

<div align="center">

🚀 **[battle-ranker.fly.dev](https://battle-ranker.fly.dev)**

</div>

---

## ✨ 주요 기능 (Key Features)

### 1. ⚔️ Battle Arena (다차원 동시 투표 시스템)
- **다중 기준 동시 평가**: 한 번의 대결에서 사용자가 설정한 모든 평가 기준(예: 스토리, 작화, 음악 등)을 동시에 비교하여 투표합니다. 이를 통해 점수 수렴 속도가 대폭 향상됩니다.
- **불확실성 기반 매치메이킹**: 가장 불확실한 항목을 우선 매칭하여 정보 획득을 극대화하고, 비슷한 복합 점수의 상대와 매칭하여 공정성을 유지합니다.
- **Focus Mode**: 특정 항목만 고정해두고 다른 항목들과 연속으로 대결시킬 수 있습니다.

### 2. 🏆 Ranking Board (동적 순위표)
- **실시간 랭킹**: 투표 즉시 Bayesian BT 사후분포가 갱신되어 순위에 반영됩니다.
- **불확실성 시각화**: 각 항목의 신뢰 구간을 표시하여 순위의 신뢰도를 직관적으로 확인할 수 있습니다. 데이터가 부족한 항목에는 "불확실" 배지가 표시됩니다.
- **가중치 기반 종합 점수**: 각 평가 기준별로 가중치를 부여하여 보다 합리적인 종합 점수를 산출합니다.
- **차트 시각화**: Chart.js를 활용하여 현재 점수 분포(Distribution)를 기준별로 한눈에 파악할 수 있습니다.

### 3. ⚙️ Management (데이터 및 시스템 설정)
- **항목(Items) 관리**: 평가할 대상을 개별 또는 여러 줄 텍스트로 일괄(Bulk) 등록하고 수정/삭제할 수 있습니다.
- **평가 기준(Criteria) 편집**: 평가할 기준의 이름, 테마 색상, 가중치를 자유롭게 추가하고 편집할 수 있습니다.
- **BT Settings**: 사전분포(Prior), 무승부 확률, 계층적 축소(Hierarchical Shrinkage), 표시 스케일 등 랭킹 알고리즘의 모든 파라미터를 UI에서 직접 튜닝할 수 있습니다.
- **데이터 백업 및 복구 (Data I/O)**: 현재 세션의 모든 데이터(설정, 기준, 항목)를 단일 `JSON` 파일로 다운로드(Export)하거나 업로드(Import)하여 이어서 진행할 수 있습니다. 이전 Elo 형식 JSON도 자동 마이그레이션됩니다.

### 4. 🗂️ 독립적인 멀티 유저 세션 (Multi-Session)
- 복잡한 DB 설정이나 회원가입 없이, 사이트 접속 시 발급되는 브라우저 쿠키를 기반으로 각 유저마다 고유한 JSON 데이터 환경을 제공합니다.

---

## 🛠 기술 스택 (Tech Stack)

- **Backend**: Python 3.13, FastAPI, aiofiles, Pydantic v2
- **Data Storage**: Local JSON Files (`store.py`, per-session cache + async file sync)
- **Frontend**: Jinja2 Templates, TailwindCSS v4 CLI build, Chart.js (jsDelivr CDN)
- **Deployment**: Fly.io (Docker container + mounted volume)

---

## 🚀 설치 및 실행 (Installation)

### 1. 클론
```bash
git clone https://github.com/RememberIOm/battle-ranker.git
cd battle-ranker
```

### 2. 개발 서버 실행
환경 변수나 DB 초기화 설정 없이 바로 실행 가능합니다. 데이터는 `ranker_data` Docker 볼륨에 저장됩니다.
```bash
docker compose up --build
```
브라우저에서 `http://localhost:8080`으로 접속하여 **"새로 시작"**을 클릭하면 즉시 사용할 수 있습니다.

> Python 의존성 추가 시: `uv add <패키지명>`
>
> Tailwind 의존성 변경 시: `npm install <패키지명> --save`

### 3. 테스트 실행
가장 간단한 검증 경로는 Docker 이미지 안에서 테스트를 실행하는 것입니다.
```bash
docker build -t ranker-test .
docker run --rm ranker-test python -m unittest discover -s tests -p 'test_*.py'
```

---

## 📂 프로젝트 구조 (Project Structure)

```text
.
├── main.py              # 앱 진입점 및 세션 쿠키 관리 라우터
├── deps.py              # FastAPI 의존성 (세션 ID 검증 및 Store 주입)
├── store.py             # 세션별 JSON 데이터 읽기/쓰기 및 캐싱 로직
├── schemas.py           # Vote / Import / Response 검증 스키마
├── services.py          # 순수 비즈니스 로직 (Bayesian BT, 매치메이킹, 계층적 축소)
├── template_env.py      # 공용 Jinja2 템플릿 환경
├── routers/             # API 라우터 모듈
│   ├── battle.py        # 대결 페이지 및 투표 처리
│   ├── ranking.py       # 순위 조회 및 통계 차트
│   └── manage.py        # 데이터 CRUD 및 시스템 파라미터 설정
├── templates/           # Jinja2 HTML 템플릿
│   ├── base.html        # 레이아웃 및 다크모드, 네비게이션
│   ├── index.html       # 메인(시작/업로드) 페이지
│   ├── battle.html      # 1:1 다중 투표 UI
│   ├── ranking.html     # 랭킹 테이블 및 차트
│   └── manage.html      # 항목/기준/설정 관리 UI
├── Dockerfile           # 프로덕션 이미지 (Fly.io 배포용)
├── Dockerfile.dev       # 개발 이미지 (hot reload, docker compose 전용)
├── docker-compose.yml   # 로컬 개발 환경 오케스트레이션
├── package.json         # Tailwind CLI 스크립트 및 Node 의존성
├── package-lock.json    # Tailwind 의존성 잠금 파일
├── pyproject.toml       # 프로젝트 메타데이터 및 의존성 (uv)
├── uv.lock              # 의존성 잠금 파일
├── tests/               # 기본 회귀 테스트
└── fly.toml             # Fly.io 배포 설정
```

---

## 🧠 알고리즘 상세 (Algorithm Logic)

모든 알고리즘 동작 방식은 **[Manage] -> [Settings]** 탭에서 실시간으로 조정할 수 있습니다.

1. **Online Bayesian Bradley-Terry (Laplace Approximation)**:
   - 각 항목·기준별로 사후분포 `(μ, σ²)`를 유지합니다. μ는 실력 추정치, σ²는 불확실성입니다.
   - 투표 시 `p = sigmoid(μ_a - μ_b)` 기반 예측 확률로 정밀도와 평균을 동시 업데이트합니다.
   - 새 항목은 높은 σ²(높은 불확실성)로 시작하여 초반에 점수가 크게 변동하고, 대결이 누적될수록 σ²가 감소하여 자연 안정화됩니다.
2. **Hierarchical Shrinkage (계층적 축소)**:
   - 투표 후 기준 간 정밀도 가중 평균을 계산하고 각 기준의 μ를 그 방향으로 축소합니다.
   - 데이터가 부족한 기준에서 다른 기준의 정보를 차용하여 보다 안정적인 추정을 제공합니다.
3. **Draw Probability (무승부 확률 보정)**:
   - Bayesian Beta prior로 실측 무승부 비율에 자연 수렴하는 드로우 확률 모델을 사용합니다.
   - 점수 차이 기반 가우시안 감쇠로 점수가 비슷할수록 높은 무승부 확률을 표시합니다.
4. **Display Conversion (표시 변환)**:
   - 내부 logit 스케일 점수를 `μ × display_scale + display_center` (기본: 173.72 × μ + 1200)로 친숙한 스케일로 변환하여 표시합니다.

---

## ☁️ 배포 (Deployment)

이 프로젝트는 **Fly.io** 배포에 최적화되어 있습니다. (Docker 컨테이너 환경)

1. `flyctl` 설치 및 로그인.
2. 앱 런칭: `fly launch`
3. 배포 진행: `fly deploy`

주의:

- `fly.toml`에 정의된 대로 `[mounts]`를 통해 Fly Volume을 `/data` 경로에 마운트해야 사용자의 JSON 세션 파일들이 서버 재시작 후에도 유지됩니다.
- 현재 저장소 계층은 프로세스 로컬 메모리 캐시와 락을 사용하므로 **단일 uvicorn 워커** 전제를 둡니다. 프로덕션 Dockerfile은 이를 위해 `--workers 1`을 명시합니다.
- HTTPS 배포에서는 `COOKIE_SECURE=true`를 사용해야 하며, 기본 Fly 설정에는 이 값이 포함되어 있습니다.

## 🎨 프런트엔드 빌드

- 프로덕션 이미지는 `package.json`과 `package-lock.json`을 복사한 뒤 `npm ci`로 Tailwind CLI 의존성을 설치합니다.
- CSS는 `npm run build:css`로 `/static/output.css`를 생성합니다.
- 로컬 개발에서는 `docker compose`의 `tailwind` 서비스가 `npm ci` 후 `npm run build:css:watch`를 실행합니다.

## ✅ 입력 검증

- `/battle/vote`는 Pydantic 스키마와 서버 발급 라운드 토큰으로 검증됩니다.
- JSON import는 `schemas.py`의 세션 스키마를 통해 settings, criteria, items 전체를 검증합니다.
- 잘못된 투표 페이로드, 누락된 rating key, 중복 item id 같은 데이터는 저장 전에 거부됩니다.

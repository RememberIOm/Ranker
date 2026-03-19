# Ranker (Session-based Elo Rating System)

**Ranker**는 1:1 대결 투표를 통해 실시간으로 순위를 산정하는 범용 랭킹 웹 애플리케이션입니다.
영화, 음식, 게임, 애니메이션 등 **어떤 주제든** 사용자가 원하는 항목과 평가 기준을 자유롭게 설정하여 나만의 랭킹을 만들 수 있습니다. 

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
- **Smart Matchmaking**: 비슷한 점수대의 '라이벌'을 매칭하여 대결의 긴장감과 랭킹의 정확도를 높입니다 (확률 조정 가능).
- **Focus Mode**: 특정 항목만 고정해두고 다른 항목들과 연속으로 대결시킬 수 있습니다.

### 2. 🏆 Ranking Board (동적 순위표)
- **실시간 랭킹**: 투표 즉시 Elo 점수가 계산되어 순위에 반영됩니다.
- **가중치 기반 종합 점수**: 각 평가 기준별로 가중치를 부여하여 보다 합리적인 종합 점수를 산출합니다.
- **차트 시각화**: Chart.js를 활용하여 현재 점수 분포(Distribution)를 기준별로 한눈에 파악할 수 있습니다.

### 3. ⚙️ Management (데이터 및 시스템 설정)
- **항목(Items) 관리**: 평가할 대상을 개별 또는 여러 줄 텍스트로 일괄(Bulk) 등록하고 수정/삭제할 수 있습니다.
- **평가 기준(Criteria) 편집**: 평가할 기준의 이름, 테마 색상, 가중치를 자유롭게 추가하고 편집할 수 있습니다.
- **Elo Settings**: K-Factor(점수 변동폭), 매치메이킹 범위, 무승부 확률, 점수 인플레이션 방지(정규화) 등 랭킹 알고리즘의 모든 파라미터를 UI에서 직접 튜닝할 수 있습니다.
- **데이터 백업 및 복구 (Data I/O)**: 현재 세션의 모든 데이터(설정, 기준, 항목)를 단일 `JSON` 파일로 다운로드(Export)하거나 업로드(Import)하여 이어서 진행할 수 있습니다.

### 4. 🗂️ 독립적인 멀티 유저 세션 (Multi-Session)
- 복잡한 DB 설정이나 회원가입 없이, 사이트 접속 시 발급되는 브라우저 쿠키를 기반으로 각 유저마다 고유한 JSON 데이터 환경을 제공합니다.

---

## 🛠 기술 스택 (Tech Stack)

- **Backend**: Python 3.13, FastAPI, aiofiles (비동기 파일 I/O)
- **Data Storage**: Local JSON Files (`store.py`, In-memory Caching + Async File Sync)
- **Frontend**: Jinja2 Templates, TailwindCSS v4 (CDN Browser Bundle), Chart.js
- **Deployment**: Fly.io (Docker container with Volume Mount)

---

## 🚀 설치 및 실행 (Installation)

### 1. 클론 및 가상환경 설정
```bash
git clone https://github.com/RememberIOm/anime-ranker.git
cd anime-ranker

python -m venv venv
# Windows
source venv/Scripts/activate
# Mac/Linux
source venv/bin/activate
```

### 2. 의존성 패키지 설치
```bash
pip install -r requirements.txt
```

### 3. 서버 실행
환경 변수나 DB 초기화 설정 없이 바로 실행 가능합니다. (데이터는 자동으로 `./data/sessions` 폴더에 생성됩니다.)
```bash
uvicorn main:app --reload
```
브라우저에서 `http://localhost:8000`으로 접속하여 **"새로 시작"**을 클릭하면 즉시 사용할 수 있습니다.

---

## 📂 프로젝트 구조 (Project Structure)

```text
.
├── main.py              # 앱 진입점 및 세션 쿠키 관리 라우터
├── deps.py              # FastAPI 의존성 (세션 ID 검증 및 Store 주입)
├── store.py             # 세션별 JSON 데이터 읽기/쓰기 및 캐싱 로직
├── schemas.py           # Pydantic 데이터 검증 스키마
├── services.py          # 순수 비즈니스 로직 (Elo 연산, 매치메이킹, 정규화)
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
└── fly.toml             # Fly.io 배포 설정
```

---

## 🧠 알고리즘 상세 (Algorithm Logic)

모든 알고리즘 동작 방식은 **[Manage] -> [Elo Settings]** 탭에서 실시간으로 조정할 수 있습니다.

1. **Dynamic K-Factor (동적 변동폭)**:
   - 처음 추가된 항목은 배치고사를 치르듯 점수가 크게 변동합니다 (`K Max`).
   - 대결 횟수가 누적될수록 점수 변동폭이 점차 줄어들어 안정화됩니다 (`K Min`, `Decay Factor`).
2. **Draw Probability (무승부 확률 보정)**:
   - 두 항목의 점수 차이에 기반하여 가상의 무승부 확률(Gaussian Curve)을 계산하고, 이를 기반으로 승패에 따른 기대 점수를 보정합니다.
3. **Inflation Control (점수 정규화)**:
   - 투표가 진행됨에 따라 전체 점수가 상향 또는 하향 평준화되는 것을 막기 위해, 백그라운드에서 주기적으로 전체 평균을 목표 점수(`Normalize Target`)로 자동 보정합니다.

---

## ☁️ 배포 (Deployment)

이 프로젝트는 **Fly.io** 배포에 최적화되어 있습니다. (Docker 컨테이너 환경)

1. `flyctl` 설치 및 로그인.
2. 앱 런칭: `fly launch`
3. 배포 진행: `fly deploy`

*주의: `fly.toml`에 정의된 대로 `[mounts]`를 통해 Fly Volume을 `/data` 경로에 마운트해야 사용자의 JSON 세션 파일들이 서버 재시작 후에도 날아가지 않고 유지됩니다.*
# Dependabot은 FROM만 읽으므로 uv 이미지를 별도 단계로 둡니다.
FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

# ── Stage 1: CSS 빌드 ──────────────────────────────────────────
FROM node:26.12.0-alpine@sha256:6f08a523443ddedf52941874c203475b0ad503e859ad70cb26ab8be9a517dbd5 AS css-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY frontend/ frontend/
COPY ranker/templates/ ranker/templates/
COPY ranker/static/*.js ranker/static/
RUN npm run build:css

# ── Stage 2: Python 런타임 ─────────────────────────────────────
FROM python:3.15.0-slim@sha256:f2dbcc4d0a7ff47b0f35724b56f778ccf0460eb119542f15444ab61ddc416435

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# 의존성 파일 먼저 복사 (레이어 캐시 활용)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY LICENSE ./
COPY ranker/ ranker/

# 사전 빌드된 CSS 복사
COPY --from=css-builder /build/ranker/static/output.css /app/ranker/static/output.css

# 세션 데이터 디렉토리 (Fly Volume이 /data에 마운트됨)
RUN useradd --system --uid 1000 --home /app --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
ENV DATABASE_PATH=/data/ranker.db
ENV PATH="/app/.venv/bin:$PATH"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

# entrypoint.sh가 볼륨 소유권 보정 후 appuser로 권한 강하합니다.
# ranker/store.py의 세션 락은 프로세스 로컬 상태이므로 단일 워커를 명시합니다.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "ranker.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

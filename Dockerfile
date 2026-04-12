# ── Stage 1: CSS 빌드 ──────────────────────────────────────────
FROM node:22-alpine AS css-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY input.css .
COPY templates/ templates/
RUN mkdir -p static && npm run build:css

# ── Stage 2: Python 런타임 ─────────────────────────────────────
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/

WORKDIR /app

# 의존성 파일 먼저 복사 (레이어 캐시 활용)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY . .

# 사전 빌드된 CSS 복사
COPY --from=css-builder /build/static/output.css /app/static/output.css

# 세션 데이터 디렉토리 (Fly Volume이 /data에 마운트됨)
RUN useradd --system --uid 1000 --home /app --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/sessions \
    && chown -R appuser:appuser /app /data
ENV SESSION_DIR=/data/sessions
ENV PATH="/app/.venv/bin:$PATH"

USER appuser
EXPOSE 8080

# store.py의 세션 캐시/락은 프로세스 로컬 상태이므로 단일 워커를 명시합니다.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

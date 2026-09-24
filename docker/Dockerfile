# ── Stage 1: CSS 빌드 ──────────────────────────────────────────
FROM node:26.10.0-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS css-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY frontend/ frontend/
COPY ranker/templates/ ranker/templates/
COPY ranker/static/*.js ranker/static/
RUN npm run build:css

# ── Stage 2: Python 런타임 ─────────────────────────────────────
FROM python:3.14.7-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /uvx /bin/

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

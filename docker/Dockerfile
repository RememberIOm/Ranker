# Dependabot은 FROM만 읽으므로 uv 이미지를 별도 단계로 둡니다.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

# ── Stage 1: CSS 빌드 ──────────────────────────────────────────
FROM node:26.13.1-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS css-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY frontend/ frontend/
COPY ranker/templates/ ranker/templates/
COPY ranker/static/*.js ranker/static/
RUN npm run build:css

# ── Stage 2: Python 런타임 ─────────────────────────────────────
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

# 의존성 파일 먼저 복사 (레이어 캐시 활용, uv 바이너리는 런타임 이미지에 남기지 않음)
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv uv sync --frozen --no-dev --no-editable

COPY LICENSE ./
COPY ranker/ ranker/
COPY scripts/__init__.py scripts/drop_redundant_indexes.py scripts/

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

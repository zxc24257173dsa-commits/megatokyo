# Production image (Phase 12): the React app is built, then served by the FastAPI backend from one origin.
# Build:  gcloud builds submit --tag <region>-docker.pkg.dev/<project>/megatokyo/erp:<tag>
# Runs on Cloud Run (listens on $PORT). Migrations run as a separate Cloud Run job:  alembic upgrade head

FROM mirror.gcr.io/library/node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run i18n:check && npm run build

FROM mirror.gcr.io/library/python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    ERP_FRONTEND_DIST=/app/frontend/dist PORT=8080
WORKDIR /app/backend
RUN useradd --create-home --uid 10001 erp
COPY backend/pyproject.toml ./
RUN pip install .
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY --from=web /web/dist /app/frontend/dist
USER erp
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 2"]

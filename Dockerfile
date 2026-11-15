FROM node:24-bookworm-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS python-build
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data \
    DOOR_API_BASE_URL=http://127.0.0.1:8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
RUN groupadd --gid 10001 door && useradd --uid 10001 --gid door --no-create-home door \
    && mkdir -p /app/data && chown door:door /app/data
COPY --from=python-build /app/.venv /app/.venv
COPY --chown=door:door --from=frontend /app/frontend/dist /app/frontend/dist
COPY --chown=door:door door/ /app/door/
COPY --chown=door:door reference_bot/ /app/reference_bot/
COPY --chown=door:door scripts/ /app/scripts/
COPY --chown=door:door LICENSE /app/LICENSE
USER door
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1
CMD ["uvicorn", "door.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

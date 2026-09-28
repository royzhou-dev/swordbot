FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# Migrations run as a release step: `alembic upgrade head` (never at app startup).
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    ENVIRONMENT=production \
    PORT=8000

EXPOSE 8000
# `exec` makes uvicorn PID 1, so it receives the host's SIGTERM on a redeploy and
# shuts down gracefully (the worker releases in-flight events). Shutdown takes at
# most ~10s for open requests plus WORKER_SHUTDOWN_GRACE_SECONDS, so the host's
# stop timeout must be longer (railway.json: drainingSeconds). No access log: the
# platform's health checks would flood it, and the app logs every event it takes.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-graceful-shutdown 10"]

# Image = Python + pinned deps only. Code (backend/, frontend/) and data stay
# bind-mounted at runtime so edits remain live — the image just removes the
# pip-install-at-boot step and enables a read-only root filesystem (S7).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # NOT /app/.venv: docker-compose.yml bind-mounts `./backend:/app:ro` at
    # runtime, which shadows anything the image put under /app — same reason
    # the old pip-based image installed to global site-packages rather than
    # a venv inside /app. Keep the venv outside the mounted path.
    UV_PROJECT_ENVIRONMENT=/venv

# uv itself, pinned to a known release rather than whatever pip resolves.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Deps are synced from a scratch dir, not /app — see UV_PROJECT_ENVIRONMENT
# above. This project (pyproject.toml: tool.uv.package = false) is deps-only
# — the backend is a flat set of scripts bind-mounted in at runtime, not an
# installable package — so a single `uv sync` is enough.
WORKDIR /deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache

ENV PATH="/venv/bin:${PATH}"

WORKDIR /app

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

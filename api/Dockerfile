# syntax=docker/dockerfile:1
#
# Production API image. The dev image is Dockerfile.dev (hot reload, source bind-mounted).
#
#   docker buildx build -f api/Dockerfile --platform linux/amd64 -t stackd-api:<version> api
#
# - Dependencies from the committed uv.lock only (`--frozen`), no dev group.
# - The dev login module (app/auth/dev.py) is DELETED from the image, not just disabled (DEV §3):
#   no setting can mount it. STACKD_ENV=production is baked in; the config refuses
#   STACKD_DEV_AUTH=true under it anyway.
# - Runs as an unprivileged user. Entrypoint (docker/entrypoint.py): `serve` (default) runs
#   uvicorn with TLS on the container port and trusts proxy headers from FORWARDED_ALLOW_IPS only;
#   `migrate` runs `alembic upgrade head` then the base seed (default space + tiers).
#
# Deploy it by digest (`<repo>@sha256:<digest>`), never by tag.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- build: the virtualenv and the app tree ---
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY docker/entrypoint.py ./entrypoint.py
# DEV §3: the dev login bypass is not shipped. Fail the build if anything still provides it.
RUN rm -f app/auth/dev.py \
    && find app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && test ! -e app/auth/dev.py \
    && ! /opt/venv/bin/python -c "import sys; sys.path.insert(0, '.'); import app.auth.dev" 2>/dev/null

# --- runtime ---
FROM ${PYTHON_IMAGE}
# git + openssh-client: the API checks repo reachability and polls branch heads (§9.6).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --uid 10001 stackd
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=root:root /app /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STACKD_ENV=production
WORKDIR /app
USER 10001
EXPOSE 8443
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["serve"]

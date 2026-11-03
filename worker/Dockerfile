# syntax=docker/dockerfile:1
#
# Production worker agent image, for the `docker` runner only (SPECS §7.4) — the one a production
# API accepts. The agent itself runs no tofu: it clones, then runs every command and hook in a
# fresh container of a runner image (deploy/images/) pinned by digest in STACKD_RUNNER_IMAGES.
# The dev image is Dockerfile.dev (local runner, tools bundled, hot reload).
#
#   docker buildx build -f worker/Dockerfile --platform linux/amd64 -t stackd-worker:<version> worker
#
# Run it on a dedicated host, with the Docker socket and the workspace root mounted at the SAME
# path on both sides (the job containers mount workspaces by host path), e.g.:
#
#   docker run --init -v /var/run/docker.sock:/var/run/docker.sock \
#     -v /var/lib/stackd/ws:/var/lib/stackd/ws -e STACKD_API_URL=https://... \
#     -e STACKD_POOL_TOKEN_FILE=... -e STACKD_RUNNER_IMAGES='{"opentofu:1.12.2": "<repo>@sha256:..."}' \
#     <repo>@sha256:<digest>
#
# It runs as root: it drives the Docker socket (root-equivalent anyway) and hands each workspace
# to the runner images' unprivileged user. `docker stop` (SIGTERM) drains the running jobs for up
# to STACKD_SHUTDOWN_TIMEOUT_SECONDS: give `docker stop -t` more than that.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agent ./agent

FROM ${PYTHON_IMAGE}
# git + openssh-client: the agent clones the job's repo. safe.directory must be system config:
# the workspace is handed to the runner user (uid 10001) while the agent (root) still runs git.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    git config --system --add safe.directory '*'
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STACKD_RUNNER=docker \
    STACKD_WORKSPACE_ROOT=/var/lib/stackd/ws
WORKDIR /app
ENTRYPOINT ["python", "-m", "agent.main"]

# syntax=docker/dockerfile:1.7
# Production worker image (docs/guide/installing.md): the agent only. It runs with the `docker`
# runner (SPECS §7.4) — every tofu/terraform command and hook runs in a fresh container of a
# runner image (deploy/images/), so no IaC binary is installed here. The dev image is
# Dockerfile.dev (local runner, OpenTofu + Terraform baked in, hot reload).
#
# The agent itself clones the repository (git, openssh-client for ssh:// and git@ URLs) and talks
# to the Docker Engine over its unix socket (no docker CLI needed).
#
# It runs as root on purpose: it hands each job's workspace to the runner images' unprivileged
# user (uid 10001) with chown before the job containers mount it. Access to the Docker socket is
# root-equivalent on the host anyway — give workers a dedicated host (see the guide).
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY agent ./agent
RUN /opt/venv/bin/python -m compileall -q agent

# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# safe.directory: after the docker runner chowns a checkout to uid 10001, the agent (root) still
# runs git in it (rev-parse); git only honours safe.directory from system/global config.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STACKD_RUNNER=docker \
    STACKD_WORKSPACE_ROOT=/var/lib/stackd/workspaces

WORKDIR /app
CMD ["python", "-m", "agent.main"]

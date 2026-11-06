# syntax=docker/dockerfile:1.7
# Production API image (docs/guide/installing.md). The dev image is Dockerfile.dev (hot reload,
# bind-mounted source, dev dependencies).
#
# - frozen dependencies from uv.lock, no dev group, no uv in the final image;
# - runs as an unprivileged user, no --reload;
# - the dev login module (app/auth/dev.py) is deleted from the image, not only gated (DEV §3);
# - the same image runs the migrations (`alembic upgrade head`, see deploy/production/compose.yml).
#
# Base images are build args so a registry mirror can be substituted; the defaults are pinned by
# digest (bump them together with the tag).
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

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
# Drop the dev login (DEV §3): the module must not exist in a production image, whatever the
# settings say. Fail the build if anything still imports it at module level.
RUN rm -f app/auth/dev.py \
    && if grep -rnE '^(from app\.auth\.dev|import app\.auth\.dev|from app\.auth import dev)' app; then \
         echo "app.auth.dev is still imported at module level" >&2; exit 1; \
       fi \
    && /opt/venv/bin/python -m compileall -q app migrations

# ---------------------------------------------------------------------------------------------
# Optional: Proton Pass `pass-cli`, the binary the proton_pass secret source runs on the API
# (SPECS §15.7). Off by default — set PASS_CLI_URL to the release binary for your architecture and
# PASS_CLI_SHA256 to its published checksum; the build fails on a URL without a checksum or on a
# mismatch (supply chain, §7.4). Without it, a proton_pass reference fails closed (§15.2).
FROM ${PYTHON_IMAGE} AS passcli
ARG PASS_CLI_URL=""
ARG PASS_CLI_SHA256=""
RUN mkdir -p /out \
    && if [ -n "$PASS_CLI_URL" ]; then \
         [ -n "$PASS_CLI_SHA256" ] || { echo "PASS_CLI_URL needs PASS_CLI_SHA256" >&2; exit 1; }; \
         python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], "/out/pass-cli")' "$PASS_CLI_URL" \
         && echo "$PASS_CLI_SHA256  /out/pass-cli" | sha256sum -c - \
         && chmod 0755 /out/pass-cli; \
       fi

# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# git: repo reachability checks, staleness polling (ls-remote) and input discovery clones;
# openssh-client: ssh:// and git@ repos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10002 stackd \
    && useradd --system --uid 10002 --gid stackd --home-dir /home/stackd --create-home \
         --shell /usr/sbin/nologin stackd

COPY --from=passcli /out/ /usr/local/bin/
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=root:root /app /app

# WEB_CONCURRENCY / FORWARDED_ALLOW_IPS are read by uvicorn: the worker process count, and which
# peers may set X-Forwarded-* (the reverse proxy — the compose file sets the edge network subnet).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STACKD_ENV=production \
    WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app
USER stackd
EXPOSE 8000

# Exec form, no shell: uvicorn is PID 1 and receives SIGTERM directly (run with `init: true` so
# git subprocesses are reaped). The healthcheck needs no curl in the image.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "30"]

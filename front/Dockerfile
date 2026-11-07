# syntax=docker/dockerfile:1.7
# Production front image (docs/guide/installing.md): the built SPA served as static files by Caddy
# on :8080, unprivileged, with the SPA fallback to index.html. TLS and the routing of /api,
# /worker, /state and the OIDC issuer to the API belong to the reverse proxy in front of it
# (deploy/production/Caddyfile) — this image only serves files, so it fits behind any ingress.
#
# Why Caddy rather than nginx: the reference deployment already uses Caddy as its TLS edge
# (automatic HTTPS, WebSockets and arbitrary methods such as the state backend's LOCK/UNLOCK
# proxied with no extra config), so one web server, one config language. `try_files` gives the
# SPA fallback in one line, and the image runs as non-root on a high port with no tuning.
ARG NODE_IMAGE=node:22.23.4-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
ARG CADDY_IMAGE=caddy:2.11.9-alpine@sha256:71d41fe4b1ae92a058abc87afe614178b996d8a6b96d0f4c71ae30606e5927e3

# The build stage runs on the build host's platform: its output (static files) is the same for
# every target, so a multi-arch build (release.yml) compiles the SPA once, natively, not under QEMU.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build

# pnpm comes from package.json's `packageManager` via corepack (pinned version, no global install).
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PNPM_HOME=/pnpm \
    CI=true
RUN corepack enable

WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# ---------------------------------------------------------------------------------------------
FROM ${CADDY_IMAGE} AS runtime

RUN addgroup -S -g 10003 web \
    && adduser -S -D -H -u 10003 -G web web \
    && chown -R web:web /data /config

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv

USER web
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/index.html"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]

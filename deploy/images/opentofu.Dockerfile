# syntax=docker/dockerfile:1
#
# OpenTofu runner image for Stackd workers (prod Docker runner, §worker protocol).
# Best practices: multi-stage (build tools never reach the final image), pinned version,
# checksum-verified download, minimal runtime, non-root user, multi-arch via buildx TARGETARCH.
#
#   docker buildx build -f deploy/images/opentofu.Dockerfile \
#     --build-arg OPENTOFU_VERSION=1.12.0 \
#     --platform linux/amd64,linux/arm64 -t stackd/opentofu:1.12.0 .

ARG OPENTOFU_VERSION=1.12.0

# --- stage 1: fetch + verify the binary (throwaway) ---
FROM alpine:3.20 AS fetch
ARG OPENTOFU_VERSION
ARG TARGETARCH
RUN apk add --no-cache curl unzip
WORKDIR /tmp
RUN set -eux; \
    base="https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}"; \
    asset="tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip"; \
    curl -fsSL "${base}/${asset}" -o "${asset}"; \
    curl -fsSL "${base}/tofu_${OPENTOFU_VERSION}_SHA256SUMS" -o SHA256SUMS; \
    # Verify integrity against the published checksums before trusting the binary (the checksum
    # line names the asset, so keep that filename on disk for `sha256sum -c`).
    grep "  ${asset}\$" SHA256SUMS | sha256sum -c -; \
    unzip "${asset}" tofu -d /out; \
    /out/tofu version

# --- stage 2: minimal runtime ---
FROM debian:bookworm-slim AS runtime
ARG OPENTOFU_VERSION
LABEL org.opencontainers.image.title="stackd-opentofu-runner" \
      org.opencontainers.image.description="OpenTofu runner image for Stackd workers" \
      org.opencontainers.image.version="${OPENTOFU_VERSION}" \
      org.opencontainers.image.source="https://github.com/fmiquel90/stackd"

# git (clone), ca-certificates (TLS to providers/registries), openssh-client (deploy keys), and a
# shell for repo/platform hooks. safe.directory must be system config (git ignores -c for it).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates openssh-client; \
    rm -rf /var/lib/apt/lists/*; \
    git config --system --add safe.directory '*'; \
    useradd --create-home --uid 10001 runner

COPY --from=fetch /out/tofu /usr/local/bin/tofu

USER runner
WORKDIR /workspace
ENTRYPOINT ["tofu"]
CMD ["version"]

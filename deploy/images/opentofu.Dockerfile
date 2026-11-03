# syntax=docker/dockerfile:1
#
# OpenTofu runner image for Stackd's `docker` runner (SPECS §7.4): every command of a job — tofu and
# hooks — runs in a fresh container of this image. Multi-stage (download tooling never reaches the
# final image), pinned version, release verified by its Sigstore signature then by checksum,
# minimal runtime, non-root user, multi-arch via buildx TARGETARCH.
#
#   docker buildx build -f deploy/images/opentofu.Dockerfile \
#     --build-arg OPENTOFU_VERSION=1.12.2 \
#     --platform linux/amd64,linux/arm64 -t stackd/opentofu:1.12.2 .
#
# The worker runs it only by digest (STACKD_RUNNER_IMAGES, §7.4): push it, then pin the digest.

ARG OPENTOFU_VERSION=1.12.2
ARG COSIGN_VERSION=v2.4.1
ARG JQ_VERSION=1.7.1

FROM ghcr.io/sigstore/cosign/cosign:${COSIGN_VERSION} AS cosign

# --- stage 1: fetch + verify the binaries (throwaway) ---
FROM alpine:3.20 AS fetch
ARG OPENTOFU_VERSION
ARG JQ_VERSION
ARG TARGETARCH
RUN apk add --no-cache curl unzip
COPY --from=cosign /ko-app/cosign /usr/local/bin/cosign
WORKDIR /tmp
RUN set -eux; \
    base="https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}"; \
    sums="tofu_${OPENTOFU_VERSION}_SHA256SUMS"; \
    asset="tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip"; \
    for f in "${asset}" "${sums}" "${sums}.sig" "${sums}.pem"; do \
      curl -fsSL "${base}/${f}" -o "${f}"; \
    done; \
    # The checksum file is only as trustworthy as its origin: verify it was signed by OpenTofu's
    # release workflow for this minor series (keyless Sigstore, GitHub Actions OIDC issuer)…
    series="$(echo "${OPENTOFU_VERSION}" | cut -d. -f1,2)"; \
    cosign verify-blob \
      --certificate-identity "https://github.com/opentofu/opentofu/.github/workflows/release.yml@refs/heads/v${series}" \
      --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
      --signature "${sums}.sig" --certificate "${sums}.pem" "${sums}"; \
    # …then the zip against it (the checksum line names the asset: keep that filename on disk).
    grep "  ${asset}\$" "${sums}" | sha256sum -c -; \
    unzip "${asset}" tofu -d /out; \
    /out/tofu version; \
    # jq: the one check tool the image carries (plan.json in hooks); pinned, checksum-verified.
    jq="jq-linux-${TARGETARCH}"; \
    jqbase="https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}"; \
    curl -fsSL "${jqbase}/${jq}" -o "${jq}"; \
    curl -fsSL "${jqbase}/sha256sum.txt" -o jq.sha256; \
    grep " ${jq}\$" jq.sha256 | sha256sum -c -; \
    install -m 0755 "${jq}" /out/jq

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

COPY --from=fetch /out/tofu /out/jq /usr/local/bin/

USER runner
WORKDIR /workspace
# No entrypoint: the worker names the program of each command (`tofu …`, `sh -c <hook>`).
ENTRYPOINT []
CMD ["tofu", "version"]

# syntax=docker/dockerfile:1
#
# Terraform runner image for Stackd's `docker` runner (SPECS §7.4). Same multi-stage, pinned,
# signature- then checksum-verified, non-root pattern as the OpenTofu image.
#
# NOTE: Terraform ≥ 1.6 is BUSL-licensed (PLAN §5) — OpenTofu is the default; build this only if a
# user explicitly opts into Terraform and accepts the BUSL terms.
#
#   docker buildx build -f deploy/images/terraform.Dockerfile \
#     --build-arg TERRAFORM_VERSION=1.14.9 \
#     --platform linux/amd64,linux/arm64 -t stackd/terraform:1.14.9 .

ARG TERRAFORM_VERSION=1.14.9
ARG JQ_VERSION=1.7.1
# HashiCorp's release signing key (https://www.hashicorp.com/security). The fetched key must have
# exactly this fingerprint, so a tampered key file fails the build instead of verifying anything.
ARG HASHICORP_KEY_FPR=C874011F0AB405110D02105534365D9472D7468F

# --- stage 1: fetch + verify the binaries (throwaway) ---
FROM alpine:3.20 AS fetch
ARG TERRAFORM_VERSION
ARG JQ_VERSION
ARG HASHICORP_KEY_FPR
ARG TARGETARCH
RUN apk add --no-cache curl unzip gnupg
WORKDIR /tmp
RUN set -eux; \
    base="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}"; \
    sums="terraform_${TERRAFORM_VERSION}_SHA256SUMS"; \
    asset="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    for f in "${asset}" "${sums}" "${sums}.sig"; do curl -fsSL "${base}/${f}" -o "${f}"; done; \
    export GNUPGHOME="$(mktemp -d)"; \
    curl -fsSL https://www.hashicorp.com/.well-known/pgp-key.txt | gpg --batch --import; \
    gpg --batch --list-keys --with-colons | grep -q "^fpr:::::::::${HASHICORP_KEY_FPR}:"; \
    # The checksum file must carry HashiCorp's signature, then the zip must match it.
    gpg --batch --verify "${sums}.sig" "${sums}"; \
    grep "  ${asset}\$" "${sums}" | sha256sum -c -; \
    unzip "${asset}" terraform -d /out; \
    /out/terraform version; \
    jq="jq-linux-${TARGETARCH}"; \
    jqbase="https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}"; \
    curl -fsSL "${jqbase}/${jq}" -o "${jq}"; \
    curl -fsSL "${jqbase}/sha256sum.txt" -o jq.sha256; \
    grep " ${jq}\$" jq.sha256 | sha256sum -c -; \
    install -m 0755 "${jq}" /out/jq

# --- stage 2: minimal runtime ---
FROM debian:bookworm-slim AS runtime
ARG TERRAFORM_VERSION
LABEL org.opencontainers.image.title="stackd-terraform-runner" \
      org.opencontainers.image.description="Terraform runner image for Stackd workers" \
      org.opencontainers.image.version="${TERRAFORM_VERSION}" \
      org.opencontainers.image.source="https://github.com/fmiquel90/stackd"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates openssh-client; \
    rm -rf /var/lib/apt/lists/*; \
    git config --system --add safe.directory '*'; \
    useradd --create-home --uid 10001 runner

COPY --from=fetch /out/terraform /out/jq /usr/local/bin/

USER runner
WORKDIR /workspace
# No entrypoint: the worker names the program of each command (`terraform …`, `sh -c <hook>`).
ENTRYPOINT []
CMD ["terraform", "version"]

# syntax=docker/dockerfile:1
#
# Terraform runner image for Stackd workers (prod Docker runner). Same multi-stage, pinned,
# checksum-verified, non-root pattern as the OpenTofu image.
#
# NOTE: Terraform ≥ 1.6 is BUSL-licensed (PLAN §5) — OpenTofu is the default; build this only if a
# user explicitly opts into Terraform and accepts the BUSL terms.
#
#   docker buildx build -f deploy/images/terraform.Dockerfile \
#     --build-arg TERRAFORM_VERSION=1.9.8 \
#     --platform linux/amd64,linux/arm64 -t stackd/terraform:1.9.8 .

ARG TERRAFORM_VERSION=1.9.8

# --- stage 1: fetch + verify the binary (throwaway) ---
FROM alpine:3.20 AS fetch
ARG TERRAFORM_VERSION
ARG TARGETARCH
RUN apk add --no-cache curl unzip
WORKDIR /tmp
RUN set -eux; \
    base="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}"; \
    asset="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    curl -fsSL "${base}/${asset}" -o "${asset}"; \
    curl -fsSL "${base}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" -o SHA256SUMS; \
    grep "  ${asset}\$" SHA256SUMS | sha256sum -c -; \
    unzip "${asset}" terraform -d /out; \
    /out/terraform version

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

COPY --from=fetch /out/terraform /usr/local/bin/terraform

USER runner
WORKDIR /workspace
ENTRYPOINT ["terraform"]
CMD ["version"]

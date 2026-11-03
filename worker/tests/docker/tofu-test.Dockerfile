# syntax=docker/dockerfile:1
# Test-only stand-in for a runner image (tests/test_docker_job.py): alpine (sh) + a checksum-verified
# tofu. The production images are deploy/images/*.Dockerfile.
#   docker build -f tests/docker/tofu-test.Dockerfile -t stackd-test/tofu:1.12.2 tests/docker
FROM alpine:3.20
ARG OPENTOFU_VERSION=1.12.2
ARG TARGETARCH=amd64
RUN set -eux; \
    base="https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}"; \
    asset="tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip"; \
    cd /tmp; \
    wget -q "${base}/${asset}" "${base}/tofu_${OPENTOFU_VERSION}_SHA256SUMS"; \
    grep "  ${asset}\$" "tofu_${OPENTOFU_VERSION}_SHA256SUMS" | sha256sum -c -; \
    unzip -q "${asset}" tofu -d /usr/local/bin; \
    rm -f /tmp/*

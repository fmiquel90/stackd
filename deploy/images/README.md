# Runner images (OpenTofu / Terraform)

Minimal, hardened images that carry **one IaC tool + git + jq**, built for Stackd's prod `docker`
runner, where the worker executes each job's commands (tofu and hooks) inside one of these
(SPECS §7.4). The dev worker image (`worker/Dockerfile.dev`) bundles OpenTofu and Terraform directly
for the `local` runner.

## What they follow

- **Multi-stage** — the download/verify tooling (`curl`, `unzip`, plus `cosign` or `gnupg`) lives in a
  throwaway `fetch` stage (Alpine); the runtime image (`debian:bookworm-slim`) carries only the tool
  binary + jq + git + ca-certificates + ssh client.
- **Pinned version** via `--build-arg` (`OPENTOFU_VERSION` / `TERRAFORM_VERSION`).
- **Signature-verified, then checksum-verified** — the published `SHA256SUMS` must carry the
  vendor's signature (OpenTofu: keyless Sigstore `cosign verify-blob`, certificate identity = the
  OpenTofu release workflow of that minor series; Terraform: HashiCorp's GPG key, pinned by its
  fingerprint), then the release zip is checked against it. `jq` is pinned and checksum-verified.
- **No entrypoint** — the worker names each command's program (`tofu …`, `sh -c <hook>`); the
  default `CMD` only prints the tool version.
- **Non-root** — runs as `runner` (uid 10001) in `/workspace`.
- **Multi-arch** — `TARGETARCH` (set by `docker buildx`) selects the `linux_amd64` / `linux_arm64`
  asset, so one command builds both.

## Build

```bash
# OpenTofu (default)
docker buildx build -f deploy/images/opentofu.Dockerfile \
  --build-arg OPENTOFU_VERSION=1.12.2 \
  --platform linux/amd64,linux/arm64 -t stackd/opentofu:1.12.2 .

# Terraform (BUSL ≥1.6 — opt-in; see PLAN §5)
docker buildx build -f deploy/images/terraform.Dockerfile \
  --build-arg TERRAFORM_VERSION=1.9.8 \
  --platform linux/amd64,linux/arm64 -t stackd/terraform:1.9.8 .
```

Or via the Taskfile: `task build-runner-images` (builds both for the host arch and loads them).

## Use them from a worker

Push the image, then give the worker its **digest** — never a tag (a tag can be re-pointed; the
agent refuses to start on one):

```bash
STACKD_RUNNER=docker
STACKD_RUNNER_IMAGES='{"opentofu:1.12.2": "registry.example.com/stackd/opentofu@sha256:<digest>"}'
```

The worker declares the versions it holds images for; the API only hands it runs whose stack's
`tool_version` matches one (SPECS §7.4).

## Sanity check

```bash
docker run --rm stackd/opentofu:1.12.2                    # → OpenTofu v1.12.2
docker run --rm stackd/terraform:1.9.8                     # → Terraform v1.9.8
docker run --rm stackd/opentofu:1.12.2 git --version
```

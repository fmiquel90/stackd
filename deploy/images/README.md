# Runner images (OpenTofu / Terraform)

Minimal, hardened images that carry **one IaC tool + git** for Stackd's prod Docker runner (the
worker executes `plan`/`apply` inside one of these per run). The dev worker image
(`worker/Dockerfile.dev`) bundles OpenTofu directly; these are the standalone, production-grade
runners.

## What they follow

- **Multi-stage** — the download/unzip tooling (`curl`, `unzip`) lives in a throwaway `fetch` stage;
  the runtime image carries only the tool binary + git + ca-certificates + ssh client.
- **Pinned version** via `--build-arg` (`OPENTOFU_VERSION` / `TERRAFORM_VERSION`).
- **Checksum-verified** — the release zip is checked against the published `SHA256SUMS` before use.
- **Non-root** — runs as `runner` (uid 10001) in `/workspace`.
- **Multi-arch** — `TARGETARCH` (set by `docker buildx`) selects the `linux_amd64` / `linux_arm64`
  asset, so one command builds both.

## Build

```bash
# OpenTofu (default)
docker buildx build -f deploy/images/opentofu.Dockerfile \
  --build-arg OPENTOFU_VERSION=1.12.0 \
  --platform linux/amd64,linux/arm64 -t stackd/opentofu:1.12.0 .

# Terraform (BUSL ≥1.6 — opt-in; see PLAN §5)
docker buildx build -f deploy/images/terraform.Dockerfile \
  --build-arg TERRAFORM_VERSION=1.9.8 \
  --platform linux/amd64,linux/arm64 -t stackd/terraform:1.9.8 .
```

Or via the Taskfile: `task build-runner-images` (builds both for the host arch and loads them).

## Sanity check

```bash
docker run --rm stackd/opentofu:1.12.0           # → OpenTofu v1.12.0
docker run --rm stackd/terraform:1.9.8 version   # → Terraform v1.9.8
docker run --rm --entrypoint git stackd/opentofu:1.12.0 --version
```

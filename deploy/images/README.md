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
  --build-arg TERRAFORM_VERSION=1.14.9 \
  --platform linux/amd64,linux/arm64 -t stackd/terraform:1.14.9 .
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

### From a private registry

The worker pulls a missing image through the Docker Engine API. To authenticate that pull, give it
a Docker client config file and point `STACKD_REGISTRY_AUTH_FILE` at it (mounted read-only):

```json
{
  "auths": {
    "registry.example.com:5000": {"auth": "<base64 of user:password>"},
    "https://index.docker.io/v1/": {"username": "me", "password": "<access token>"},
    "myregistry.azurecr.io": {"identitytoken": "<refresh token>"}
  }
}
```

The entry is picked by the image reference's registry host — the first path component when it has
a `.` or a `:` or is `localhost` (`registry.example.com:5000/stackd/opentofu@sha256:…`), else
Docker Hub (`docker.io`, `index.docker.io` and `https://index.docker.io/v1/` all match it) — and
sent as the `X-Registry-Auth` header of that pull only. No matching entry: an anonymous pull. The
file is re-read at each pull (a job refreshing a short-lived ECR/GAR token can rewrite it); a
missing or malformed file refuses to start the worker. The credential is never logged, is masked
out of pull errors, and never reaches a job container.

**Not supported:** credential helpers (`credsStore`, `credHelpers` — e.g. `docker-credential-ecr-login`,
Docker Desktop's store). Write the credential into `auths`, or pre-pull the images on the worker
host (`docker pull <ref>@sha256:…`), which the agent then finds locally.

## Sanity check

```bash
docker run --rm stackd/opentofu:1.12.2                    # → OpenTofu v1.12.2
docker run --rm stackd/terraform:1.14.9                     # → Terraform v1.14.9
docker run --rm stackd/opentofu:1.12.2 git --version
```

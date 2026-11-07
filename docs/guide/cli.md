# Command-line client

`stackd` is a single static binary that drives a Stackd instance over its
[REST API](../api.md): spaces, stacks, environments, runs and configuration export/import. Use it
in CI jobs and scripts instead of hand-written `curl` + `jq`. Specification: SPECS §24.

## Install

Download the archive for your OS and architecture from the repository's releases (tags
`cli-vX.Y.Z`), check it against `SHA256SUMS`, and put `stackd` on your `PATH`:

```bash
VERSION=0.1.0
curl -sSLO https://github.com/fmiquel90/stackd/releases/download/cli-v$VERSION/stackd_${VERSION}_linux_amd64.tar.gz
curl -sSLO https://github.com/fmiquel90/stackd/releases/download/cli-v$VERSION/SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
tar xzf stackd_${VERSION}_linux_amd64.tar.gz
sudo install stackd_${VERSION}_linux_amd64/stackd /usr/local/bin/
```

From a checkout: `task cli-build` (writes `./bin/stackd`; needs Go).

Shell completion: `stackd completion bash|zsh|fish|powershell --help`.

## Authenticate

The CLI keeps no state: it reads the instance URL and a token from the environment (or `--url` /
`--token`).

```bash
export STACKD_URL=https://stackd.example.com
export STACKD_TOKEN=stkd_…          # a service-account API token
stackd spaces list
```

For automation, use a **service account** and its expiring token (Settings → Service accounts;
see [Authentication](../api.md#authentication)). Grant the account a membership in the space it
works in: `writer` to create stacks and trigger plans, `admin` for `config export` / `import`.

!!! note "What a token can't do, the CLI can't either"
    The CLI adds no right. A service account **never confirms** an apply or a destroy, so there
    is no `stackd runs confirm`: a plan triggered from CI waits for a named human in the UI (or
    the environment's autodeploy). Creating a **space** requires an instance admin, which a
    service account can never be: `stackd spaces create` only works with an admin's session token.

The token is never printed, including in `--help`. Pass secrets on stdin, never as a flag value.

## Name things

| Object | By id | By name |
|---|---|---|
| Space | `01933…` | `platform` |
| Stack | `01933…` | `network` (add `--space platform` if that name exists in several spaces) |
| Environment | `01933…` | `network/prod` (`<stack>/<environment>`) |
| Run | `01933…` | — |

## Common tasks

Create a stack and its environments:

```bash
stackd stacks create network --space platform \
  --repo-url https://github.com/acme/infra.git --project-root network --tool-version 1.10

# A private repository: the token comes from stdin, never from a flag
printf %s "$REPO_TOKEN" | stackd stacks update network --repo-auth-kind token --repo-secret-stdin

stackd envs create network dev  --tier dev  --branch main --autodeploy
stackd envs create network prod --tier prod --branch main --protected
```

Trigger a plan from CI and follow it:

```bash
stackd runs plan network/prod --wait --logs
```

`--wait` returns once the plan **awaits confirmation** (`unconfirmed`) or the run ends. Exit
status: `0` at `unconfirmed` or `finished`, `2` if the run failed, was canceled or discarded, `1`
on any other error (API error, `--timeout`, default 30 min). Other plan options: `--destroy`,
`--target <addr>` / `--replace <addr>` (repeatable), `--commit <sha>`, `--with-downstream`.

Inspect runs:

```bash
stackd runs list network/prod
stackd runs get  <run-id>
stackd runs logs <run-id> -f      # follow until the run ends or awaits confirmation
stackd runs cancel  <run-id>
stackd runs discard <run-id>      # drop a plan awaiting confirmation
```

Version a space's configuration in Git ([Backup & restore](backup-restore.md) explains the
document):

```bash
stackd config export platform -f platform.json
stackd config import platform platform.json --dry-run   # the report, nothing written
stackd config import platform platform.json
```

The import merges by name: it creates what is missing and updates what differs. It never
deletes. Secrets are never in the document: the report lists the ones to fill in.

## Output for scripts

`--output json` (or `STACKD_OUTPUT=json`) prints the API's own response body, unmodified:

```bash
ENV_ID=$(stackd envs get network/prod -o json | jq -r .id)
stackd runs plan network/prod -o json | jq -r .id
```

Every other output is meant for people and may change between versions.

## A GitHub Actions job

```yaml
plan:
  runs-on: ubuntu-latest
  env:
    STACKD_URL: https://stackd.example.com
    STACKD_TOKEN: ${{ secrets.STACKD_TOKEN }}
  steps:
    - run: |
        curl -sSL https://github.com/fmiquel90/stackd/releases/download/cli-v0.1.0/stackd_0.1.0_linux_amd64.tar.gz | tar xz
        ./stackd_0.1.0_linux_amd64/stackd runs plan network/prod --wait --logs
```

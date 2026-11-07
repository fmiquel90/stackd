# stackd CLI

Command-line client for the Stackd API — a single static Go binary. Spec: `docs/SPECS.md` §24;
user guide: `docs/guide/cli.md`.

```bash
task cli-build          # → cli/dist/stackd
task cli-test           # gofmt + go vet + go test
```

A standalone Go module (`go.mod` here): it shares no code with `api/` and only speaks the public
REST API, so it works against any instance. Releases: push a `cli-vX.Y.Z` tag
(`.github/workflows/cli-release.yml`).

The asdf plugin lives at the repository root (`bin/`, where asdf expects it):
`asdf plugin add stackd https://github.com/fmiquel90/stackd.git`. Test it with
`task asdf-plugin-test`.

#!/usr/bin/env bash
# Checks that every place carrying the Stackd version agrees (and, given an argument, that it is
# that version). Run by CI on every push and by the release workflow against the pushed tag.
#
#   scripts/check-version.sh            # all files agree
#   scripts/check-version.sh 1.4.0      # all files agree and say 1.4.0 (a leading `v` is ignored)
#
# The version lives in: api/pyproject.toml, api/uv.lock, api/app/__init__.py,
# worker/pyproject.toml, worker/uv.lock, front/package.json. scripts/release.sh bumps them all.
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${1:-}"
expected="${expected#v}"

# The first `version = "…"` of a pyproject.toml is the [project] one (it is the first table).
pyproject() { sed -n 's/^version = "\(.*\)"$/\1/p' "$1" | head -n1; }
# In a uv.lock, the line after the project's own `name = "…"`.
uvlock() { sed -n "/^name = \"$2\"\$/{n;s/^version = \"\(.*\)\"\$/\1/p;}" "$1"; }

declare -A found=(
  [api/pyproject.toml]="$(pyproject api/pyproject.toml)"
  [api/uv.lock]="$(uvlock api/uv.lock stackd-api)"
  [api/app/__init__.py]="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' api/app/__init__.py)"
  [worker/pyproject.toml]="$(pyproject worker/pyproject.toml)"
  [worker/uv.lock]="$(uvlock worker/uv.lock stackd-worker)"
  [front/package.json]="$(sed -n 's/^  "version": "\(.*\)",$/\1/p' front/package.json)"
)

reference="${expected:-${found[api/pyproject.toml]}}"
status=0
for file in "${!found[@]}"; do
  if [[ -z "${found[$file]}" ]]; then
    echo "check-version: no version found in $file" >&2
    status=1
  elif [[ "${found[$file]}" != "$reference" ]]; then
    echo "check-version: $file says ${found[$file]}, expected $reference" >&2
    status=1
  fi
done
[[ $status -eq 0 ]] && echo "check-version: $reference everywhere"
exit $status

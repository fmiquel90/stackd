#!/usr/bin/env bash
# Cuts a Stackd release: bumps the version everywhere, commits, and tags — it never pushes.
# Pushing the `vX.Y.Z` tag starts .github/workflows/release.yml (CI + e2e, then the signed images
# on ghcr.io and the GitHub release). See docs/DEV.md §10.
#
#   scripts/release.sh 1.4.0          # or: task release VERSION=1.4.0
#   scripts/release.sh 1.5.0-rc.1     # a pre-release: published, never tagged `latest`
#
# On `main` (up to date with origin/main) it commits and tags; push both with
#   git push --atomic origin main v1.4.0
# On any other branch it only commits the bump: merge it through a pull request, then tag the
# merge commit on main (the release workflow refuses a tag that is not on main).
set -euo pipefail
cd "$(dirname "$0")/.."

version="${1:-}"
version="${version#v}"
semver='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
if [[ ! "$version" =~ $semver ]]; then
  echo "usage: scripts/release.sh X.Y.Z[-pre]   (got '${1:-}')" >&2
  exit 2
fi
tag="v$version"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "release: the working tree is not clean — commit or stash first" >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "release: tag $tag already exists" >&2
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" == "main" ]]; then
  git fetch -q origin main
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
    echo "release: main is not in sync with origin/main — pull (or push) first" >&2
    exit 1
  fi
fi

# Same files as scripts/check-version.sh.
sed -i "0,/^version = \".*\"\$/s//version = \"$version\"/" api/pyproject.toml worker/pyproject.toml
sed -i "/^name = \"stackd-api\"\$/{n;s/^version = \".*\"\$/version = \"$version\"/;}" api/uv.lock
sed -i "/^name = \"stackd-worker\"\$/{n;s/^version = \".*\"\$/version = \"$version\"/;}" worker/uv.lock
sed -i "s/^__version__ = \".*\"\$/__version__ = \"$version\"/" api/app/__init__.py
sed -i "0,/^  \"version\": \".*\",\$/s//  \"version\": \"$version\",/" front/package.json
scripts/check-version.sh "$version"

git add api/pyproject.toml api/uv.lock api/app/__init__.py worker/pyproject.toml worker/uv.lock \
  front/package.json
git commit -q -m "chore(release): $tag"

if [[ "$branch" == "main" ]]; then
  git tag -a "$tag" -m "Stackd $tag"
  echo "release: committed and tagged $tag. Publish it with:"
  echo "  git push --atomic origin main $tag"
else
  echo "release: committed the $tag bump on '$branch'. Merge it into main, then:"
  echo "  git fetch origin main && git tag -a $tag -m 'Stackd $tag' origin/main && git push origin $tag"
fi

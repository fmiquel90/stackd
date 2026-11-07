# shellcheck shell=bash
# Shared helpers for the stackd asdf plugin (SPECS §24). asdf runs the scripts of this directory
# (list-all, latest-stable, download, install, help.*); this file is only sourced by them.
#
# Versions are the CLI's release tags without their prefix: tag cli-v0.1.0 → asdf version 0.1.0.
# Release assets (.github/workflows/cli-release.yml): stackd_<version>_<os>_<arch>.tar.gz + SHA256SUMS.
#
# Settings (environment):
#   ASDF_STACKD_REPO          git/GitHub repository holding the CLI releases (default below)
#   ASDF_STACKD_DOWNLOAD_BASE base URL of the release assets, <base>/cli-v<version>/<asset>
#                             (a mirror or an internal artifact store; default: GitHub releases)
#   GITHUB_API_TOKEN          a GitHub token, for a private repository or to avoid rate limits

set -euo pipefail

TOOL_NAME="stackd"
TAG_PREFIX="cli-v"
DEFAULT_REPO="https://github.com/fmiquel90/stackd"
STACKD_REPO="${ASDF_STACKD_REPO:-$DEFAULT_REPO}"

fail() {
	echo "asdf-$TOOL_NAME: $*" >&2
	exit 1
}

# curl with fail-on-HTTP-error, retries and the optional GitHub token. The token goes in a config
# read from stdin, not on the command line, so it never shows in `ps`.
curl_cmd() {
	if [ -n "${GITHUB_API_TOKEN:-}" ]; then
		printf 'header = "Authorization: token %s"\n' "$GITHUB_API_TOKEN" |
			curl -fsSL --retry 3 --retry-delay 2 --config - "$@"
	else
		curl -fsSL --retry 3 --retry-delay 2 "$@"
	fi
}

# The repository as "owner/name" when it is on github.com, empty otherwise.
github_slug() {
	case "$STACKD_REPO" in
	https://github.com/*) echo "${STACKD_REPO#https://github.com/}" | sed -e 's#\.git$##' -e 's#/$##' ;;
	*) echo "" ;;
	esac
}

# Sort versions semver-wise (oldest first): pre-releases (x.y.z-rc1) sort before their release.
sort_versions() {
	sed 'h; s/[+-]/./g; s/$/.zzzzzz/; G; s/\n/ /' |
		LC_ALL=C sort -t. -k 1,1n -k 2,2n -k 3,3n -k 4,4 -k 5,5n | awk '{print $2}'
}

# Every released version, oldest first, from the repository's cli-v* tags.
list_all_versions() {
	git ls-remote --tags --refs "$STACKD_REPO" "refs/tags/${TAG_PREFIX}*" 2>/dev/null |
		sed -n "s#.*refs/tags/${TAG_PREFIX}##p" |
		grep -E '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$' |
		sort_versions || true
}

detect_os() {
	case "$(uname -s)" in
	Linux) echo linux ;;
	Darwin) echo darwin ;;
	*) fail "unsupported OS $(uname -s): download a release by hand (Windows builds are on the release page)" ;;
	esac
}

detect_arch() {
	case "$(uname -m)" in
	x86_64 | amd64) echo amd64 ;;
	aarch64 | arm64) echo arm64 ;;
	*) fail "unsupported architecture $(uname -m)" ;;
	esac
}

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | awk '{print $1}'
	else
		shasum -a 256 "$1" | awk '{print $1}'
	fi
}

# download_asset <version> <asset name> <output file>
download_asset() {
	local version="$1" asset="$2" out="$3" tag="${TAG_PREFIX}$1" slug
	if [ -n "${ASDF_STACKD_DOWNLOAD_BASE:-}" ]; then
		curl_cmd -o "$out" "${ASDF_STACKD_DOWNLOAD_BASE%/}/$tag/$asset"
		return
	fi
	slug="$(github_slug)"
	[ -n "$slug" ] || fail "ASDF_STACKD_REPO is not a github.com repository: set ASDF_STACKD_DOWNLOAD_BASE"
	if [ -n "${GITHUB_API_TOKEN:-}" ]; then
		# Through the API: works for a private repository too (a plain download URL does not).
		local id
		id="$(curl_cmd "https://api.github.com/repos/$slug/releases/tags/$tag" |
			tr -d '\n' | grep -o '"url": *"[^"]*/releases/assets/[0-9]*"[^}]*"name": *"'"$asset"'"' |
			grep -o 'assets/[0-9]*' | head -n1 | cut -d/ -f2)" || true
		[ -n "$id" ] || fail "asset $asset not found in release $tag of $slug"
		curl_cmd -H "Accept: application/octet-stream" -o "$out" \
			"https://api.github.com/repos/$slug/releases/assets/$id"
	else
		curl_cmd -o "$out" "https://github.com/$slug/releases/download/$tag/$asset"
	fi
}

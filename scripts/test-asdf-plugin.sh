#!/usr/bin/env bash
# End-to-end test of the stackd asdf plugin (bin/, SPECS §24) without asdf or GitHub: builds the
# CLI, lays out a fake release (the same assets cli-release.yml publishes) served over HTTP, tags a
# throwaway git repo, then runs the plugin's scripts with the environment asdf gives them.
# Needs go, git, curl, python3. Usage: scripts/test-asdf-plugin.sh   (task asdf-plugin-test)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
plugin="$root/bin"
work="$(mktemp -d)"
server_pid=""
cleanup() {
	[ -n "$server_pid" ] && kill "$server_pid" 2>/dev/null || true
	rm -rf "$work"
}
trap cleanup EXIT

ok() { echo "ok - $*"; }
die() {
	echo "FAIL - $*" >&2
	exit 1
}

case "$(uname -s)" in Linux) os=linux ;; Darwin) os=darwin ;; *) die "unsupported OS" ;; esac
case "$(uname -m)" in x86_64 | amd64) arch=amd64 ;; aarch64 | arm64) arch=arm64 ;; *) die "unsupported arch" ;; esac

# 1. A fake release cli-v0.2.0 (and a broken one, cli-v0.3.0, whose checksum is wrong).
release() {
	local version="$1" name="stackd_${1}_${os}_${arch}" dir="$work/releases/cli-v$1"
	mkdir -p "$dir/$name"
	(cd "$root/cli" && CGO_ENABLED=0 go build -ldflags "-X github.com/fmiquel90/stackd/cli/internal/cmd.Version=v$version" \
		-o "$dir/$name/stackd" .)
	tar -czf "$dir/$name.tar.gz" -C "$dir" "$name"
	rm -rf "${dir:?}/$name"
	(cd "$dir" && { sha256sum "$name.tar.gz" 2>/dev/null || shasum -a 256 "$name.tar.gz"; } >SHA256SUMS)
}
release 0.2.0
release 0.3.0
sed -i.bak 's/^[0-9a-f]\{8\}/00000000/' "$work/releases/cli-v0.3.0/SHA256SUMS"

port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$work/releases" >/dev/null 2>&1 &
server_pid=$!
for _ in $(seq 50); do curl -fs "http://127.0.0.1:$port/" >/dev/null && break || sleep 0.1; done

# 2. A repo whose tags are the versions asdf lists (non-CLI tags must be ignored).
git init -q "$work/repo"
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
for t in cli-v0.1.0 cli-v0.2.0 cli-v0.10.0-rc1 cli-v0.3.0 v9.9.9 cli-vbogus; do git -C "$work/repo" tag "$t"; done

export ASDF_STACKD_REPO="$work/repo"
export ASDF_STACKD_DOWNLOAD_BASE="http://127.0.0.1:$port"

# 3. list-all / latest-stable.
got="$("$plugin/list-all")"
[ "$got" = "0.1.0 0.2.0 0.3.0 0.10.0-rc1" ] || die "list-all: got '$got'"
ok "list-all sorts versions and ignores other tags"
got="$("$plugin/latest-stable")"
[ "$got" = "0.3.0" ] || die "latest-stable: got '$got'"
got="$("$plugin/latest-stable" 0.2)"
[ "$got" = "0.2.0" ] || die "latest-stable 0.2: got '$got'"
ok "latest-stable skips pre-releases and honours a prefix"

# 4. download + install of a good release.
export ASDF_INSTALL_TYPE=version ASDF_INSTALL_VERSION=0.2.0
export ASDF_DOWNLOAD_PATH="$work/dl/0.2.0" ASDF_INSTALL_PATH="$work/installs/0.2.0"
"$plugin/download" >/dev/null
"$plugin/install" >/dev/null
got="$("$work/installs/0.2.0/bin/stackd" version)"
[ "$got" = "stackd v0.2.0" ] || die "installed binary: got '$got'"
ok "download + install puts a working binary in \$ASDF_INSTALL_PATH/bin"

# 5. A checksum mismatch is refused, and nothing is extracted.
export ASDF_INSTALL_VERSION=0.3.0 ASDF_DOWNLOAD_PATH="$work/dl/0.3.0"
if out="$("$plugin/download" 2>&1)"; then die "download accepted a bad checksum"; fi
[[ "$out" == *"checksum mismatch"* ]] || die "unexpected error: $out"
[ ! -e "$work/dl/0.3.0/stackd" ] || die "a binary was extracted despite the bad checksum"
ok "a checksum mismatch is refused"

# 6. A missing release fails clearly; a ref install is refused.
export ASDF_INSTALL_VERSION=0.9.0 ASDF_DOWNLOAD_PATH="$work/dl/0.9.0"
if out="$("$plugin/download" 2>&1)"; then die "download of a missing release succeeded"; fi
[[ "$out" == *"could not download"* ]] || die "unexpected error: $out"
if ASDF_INSTALL_TYPE=ref "$plugin/download" >/dev/null 2>&1; then die "a ref install was accepted"; fi
ok "a missing release and a ref install fail clearly"

echo "all asdf plugin checks passed"

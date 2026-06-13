#!/usr/bin/env bash
# Creates the demo fixture git repos under .dev/repos (DEV §7). Idempotent: skips repos that
# already exist. The terraform uses only the built-in `terraform_data` resource — no providers
# to download, no cloud — so `tofu plan/apply` runs fully offline in the dev worker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPOS="$ROOT/.dev/repos"
mkdir -p "$REPOS"

init_repo() {
  local name="$1"
  local dir="$REPOS/$name"
  if [[ -d "$dir/.git" ]]; then
    echo "fixture repo $name already exists — skipping"
    return
  fi
  mkdir -p "$dir"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email "seed@dev.local"
  git -C "$dir" config user.name "Stackd Seed"
}

commit_repo() {
  local name="$1"
  local dir="$REPOS/$name"
  git -C "$dir" add -A
  # Only commit if there is something staged (idempotent re-runs are no-ops).
  if ! git -C "$dir" diff --cached --quiet; then
    git -C "$dir" commit -q -m "chore: seed demo fixture"
  fi
}

# --- demo-network: produces output `network_name` ---
init_repo demo-network
cat > "$REPOS/demo-network/main.tf" <<'TF'
variable "org" {
  type    = string
  default = "demo"
}

# Built-in resource (no provider download) so plan/apply runs offline.
resource "terraform_data" "network" {
  input = "${var.org}-net"
}

output "network_name" {
  value = terraform_data.network.output
}
TF
commit_repo demo-network

# --- demo-app: consumes `network_name` (real upstream output or mock) ---
init_repo demo-app
cat > "$REPOS/demo-app/main.tf" <<'TF'
variable "org" {
  type    = string
  default = "demo"
}

variable "network_name" {
  type    = string
  default = "unset"
}

resource "terraform_data" "app" {
  input = "${var.org}-app-on-${var.network_name}"
}

output "app_target" {
  value = terraform_data.app.output
}
TF
commit_repo demo-app

echo "fixture repos ready in $REPOS (demo-network, demo-app)"

#!/usr/bin/env bash
# Provisions the dev S3 (Garage) that holds managed states and saved plans (DEV §2): brings the
# garage service up, assigns its single-node layout, creates the access key and the bucket, and
# grants the key on the bucket. Idempotent: every step checks before acting.
#
# The key lives in .env (S3_ACCESS_KEY / S3_SECRET_KEY). It is generated and written there only
# when both are empty, then imported into Garage whenever Garage lacks it, so `task reset` (which
# wipes Garage's volumes but keeps .env) provisions the same key again. Compose reads .env
# (env_file) when it creates a container: run this after scripts/bootstrap-env.sh and before
# `up` creates the api container. A later `up` recreates a container whose .env values changed.
#
# COMPOSE overrides the compose invocation (default: the dev compose file).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.dev.yml}"

if [[ ! -f .env ]]; then
  echo "bootstrap-s3: .env is missing, run scripts/bootstrap-env.sh first" >&2
  exit 1
fi

# The garage CLI inside the container. RUST_LOG drops the INFO line each call logs on connect.
garage() { $COMPOSE exec -T -e RUST_LOG=warn garage /garage "$@"; }

# Value of KEY in .env: last assignment wins, inline comment and quotes stripped.
env_get() {
  awk -v k="$1" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) }
    END { sub(/[ \t]+#.*$/, "", v); gsub(/^[ \t"\047]+|[ \t"\047]+$/, "", v); print v }' .env
}

# Replace the KEY= line of .env (appended if absent). Goes through a temp file to stay portable
# (no BSD/GNU `sed -i` difference) and writes back in place, keeping the file's mode.
env_set() {
  local tmp
  tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print } END { if (!done) print k "=" v }' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

# 0. Garage up and answering its admin RPC (the compose healthcheck; the loop covers a compose
#    file without it).
$COMPOSE up -d --wait garage
for _ in $(seq 30); do
  garage status > /dev/null 2>&1 && break
  sleep 1
done
status="$(garage status)"

# 1. Single-node layout: a node without a role stores nothing. Assign one (zone dc1, 1 GB is a
#    capacity weight, not a quota) and apply it as the next layout version.
if grep -q "NO ROLE ASSIGNED" <<< "$status"; then
  node_id="$(garage node id -q | cut -d@ -f1)"
  version="$(garage layout show | sed -n 's/^Current cluster layout version: //p')"
  garage layout assign -z dc1 -c 1GB "$node_id" > /dev/null
  garage layout apply --version "$((${version:-0} + 1))" > /dev/null
  echo "bootstrap-s3: assigned the single-node layout"
fi

# 2. Access key: generated into .env only when both values are empty, in Garage's own format
#    (GK + 12 random bytes in hex; secret = 32 random bytes in hex), then imported if Garage lacks
#    it. The secret is never printed.
key_id="$(env_get S3_ACCESS_KEY)"
secret="$(env_get S3_SECRET_KEY)"
if [[ -z "$key_id" && -z "$secret" ]]; then
  key_id="GK$(openssl rand -hex 12)"
  secret="$(openssl rand -hex 32)"
  env_set S3_ACCESS_KEY "$key_id"
  env_set S3_SECRET_KEY "$secret"
  echo "bootstrap-s3: generated the dev S3 key into .env"
elif [[ -z "$key_id" || -z "$secret" ]]; then
  echo "bootstrap-s3: .env sets only one of S3_ACCESS_KEY / S3_SECRET_KEY:" \
    "set both, or empty both to generate a new key" >&2
  exit 1
fi
if ! garage key info "$key_id" > /dev/null 2>&1; then
  garage key import --yes -n stackd-dev "$key_id" "$secret" > /dev/null
  echo "bootstrap-s3: imported key $key_id into garage"
fi

# 3. The bucket the API uses (S3_BUCKET, app.config default "stackd"), and the key's grant on it.
bucket="$(env_get S3_BUCKET)"
bucket="${bucket:-stackd}"
if ! garage bucket info "$bucket" > /dev/null 2>&1; then
  garage bucket create "$bucket" > /dev/null
  echo "bootstrap-s3: created bucket $bucket"
fi
garage bucket allow --read --write --owner "$bucket" --key "$key_id" > /dev/null

echo "bootstrap-s3: dev S3 ready (bucket $bucket, key $key_id, http://localhost:3900)"

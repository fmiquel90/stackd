#!/usr/bin/env bash
# Prepares the env files of the production compose deployment (docs/guide/installing.md):
#   - copies .env.example / stackd.env.example / worker.env.example when the file is missing;
#   - generates STACKD_ENCRYPTION_KEY and STACKD_JWT_SECRET (stackd.env) when they are empty;
#   - with the bundled-postgres profile: generates POSTGRES_PASSWORD (.env) and, if DATABASE_URL
#     is empty, points it at the bundled container.
# Idempotent: it NEVER overwrites a value already set — rotating STACKD_ENCRYPTION_KEY in place
# would make every stored secret unreadable. Needs bash, openssl and awk.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
umask 077

for f in .env stackd.env worker.env; do
  if [[ ! -f "$f" ]]; then
    cp "$f.example" "$f"
    echo "created $f from $f.example"
  fi
done

# get FILE KEY → the value of the first uncommented KEY= line (empty if absent).
get() { awk -v k="$2" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"; }

# set_if_empty FILE KEY VALUE → set KEY only when its line is absent or has an empty value.
set_if_empty() {
  local file=$1 key=$2 value=$3 current
  current="$(get "$file" "$key")"
  if [[ -n "$current" ]]; then
    echo "kept $key ($file)"
    return
  fi
  if grep -q "^${key}=" "$file"; then
    awk -v k="$key" -v v="$value" \
      'index($0, k "=") == 1 && !done { print k "=" v; done = 1; next } { print }' \
      "$file" > "$file.tmp"
    mv "$file.tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
  echo "generated $key ($file)"
}

set_if_empty stackd.env STACKD_ENCRYPTION_KEY "$(openssl rand -base64 32 | tr -d '\n')"
set_if_empty stackd.env STACKD_JWT_SECRET "$(openssl rand -base64 48 | tr -d '\n')"

if [[ ",$(get .env COMPOSE_PROFILES)," == *",bundled-postgres,"* ]]; then
  # hex: safe inside a URL without encoding.
  set_if_empty .env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  set_if_empty stackd.env DATABASE_URL \
    "postgresql+asyncpg://stackd:$(get .env POSTGRES_PASSWORD)@postgres:5432/stackd"
fi

chmod 600 .env stackd.env worker.env
echo "done — review .env, stackd.env and worker.env before 'docker compose up -d'."

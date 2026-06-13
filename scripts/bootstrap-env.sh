#!/usr/bin/env bash
# Generates .env and dev secrets on first launch (DEV §2). Idempotent via Taskfile `status`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p .dev

gen_b64() { openssl rand -base64 32 | tr -d '\n'; }

if [[ ! -f .dev/encryption.key ]]; then gen_b64 > .dev/encryption.key; fi
if [[ ! -f .dev/jwt.secret ]]; then gen_b64 > .dev/jwt.secret; fi

# OIDC issuer RS256 keypair (workload identity, §10) — used from Phase 6, generated early.
if [[ ! -f .dev/oidc_private.pem ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .dev/oidc_private.pem 2>/dev/null
  openssl rsa -in .dev/oidc_private.pem -pubout -out .dev/oidc_public.pem 2>/dev/null
fi

cp .env.example .env
# Inject generated secrets portably (avoids BSD/GNU sed -i differences).
ENC_KEY="$(cat .dev/encryption.key)" \
JWT_SECRET="$(cat .dev/jwt.secret)" \
python3 - <<'PY'
import os, re
enc, jwt = os.environ["ENC_KEY"], os.environ["JWT_SECRET"]
text = open(".env").read()
text = re.sub(r"^STACKD_ENCRYPTION_KEY=.*$", f"STACKD_ENCRYPTION_KEY={enc}", text, flags=re.M)
text = re.sub(r"^STACKD_JWT_SECRET=.*$", f"STACKD_JWT_SECRET={jwt}", text, flags=re.M)
open(".env", "w").write(text)
PY

echo "Generated .env and dev secrets in .dev/"

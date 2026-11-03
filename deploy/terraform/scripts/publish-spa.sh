#!/usr/bin/env bash
# Builds the front (front/) and publishes it to the SPA bucket, then invalidates CloudFront.
#
#   deploy/terraform/scripts/publish-spa.sh <spa_bucket> <cloudfront_distribution_id>
#
# Take both values from the outputs (`tofu output -raw spa_bucket`, `... cloudfront_distribution_id`).
# Needs pnpm (corepack) and the AWS CLI v2. Set SKIP_BUILD=1 to publish an existing front/dist.
# The SPA calls the API on its own origin (/api/v1, same as in dev): no build-time setting.
set -euo pipefail

bucket="${1:?usage: publish-spa.sh <spa_bucket> <cloudfront_distribution_id>}"
distribution="${2:?usage: publish-spa.sh <spa_bucket> <cloudfront_distribution_id>}"
front="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../front" && pwd)"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  (cd "$front" && pnpm install --frozen-lockfile && pnpm build)
fi
dist="$front/dist"
[[ -f "$dist/index.html" ]] || { echo "publish-spa: $dist/index.html missing" >&2; exit 1; }

# Hashed assets first, cached for a year: the old index.html keeps working meanwhile.
if [[ -d "$dist/assets" ]]; then
  aws s3 sync "$dist/assets" "s3://$bucket/assets" \
    --cache-control "public, max-age=31536000, immutable"
fi
# Then everything else (index.html first among them), revalidated on every load.
aws s3 sync "$dist" "s3://$bucket" --exclude "assets/*" --cache-control "no-cache"
# Finally drop what the new build no longer has.
aws s3 sync "$dist" "s3://$bucket" --delete --size-only

aws cloudfront create-invalidation --distribution-id "$distribution" --paths "/*" \
  --query 'Invalidation.Id' --output text
echo "publish-spa: done"

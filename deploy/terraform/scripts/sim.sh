#!/usr/bin/env bash
# Applies the module against MiniStack and checks the result (task tf:sim):
#   1. apply from scratch,
#   2. no generated secret in the state (write-only arguments),
#   3. a second plan is empty (no perpetual diff),
#   4. destroy leaves nothing behind.
# KEEP=1 skips the destroy so you can poke at the emulator (aws --endpoint-url http://localhost:4566).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
example="$here/examples/ministack"
compose=(docker compose -f "$example/compose.yml")
tofu=(tofu -chdir="$example")

# S3 Control calls (bucket tags) go to <account id>.<endpoint host>.
if ! getent hosts 000000000000.localhost >/dev/null; then
  echo "000000000000.localhost does not resolve: add '127.0.0.1 000000000000.localhost' to /etc/hosts" >&2
  exit 1
fi

"${compose[@]}" up -d --wait

# Fresh emulator and fresh state every run: leftovers of an aborted run would collide by name.
curl -fsS -X POST http://localhost:4566/_ministack/reset >/dev/null
rm -rf "$example/.terraform" "$example/terraform.tfstate" "$example/terraform.tfstate.backup"
"${tofu[@]}" init -input=false -no-color >/dev/null

echo "── apply"
"${tofu[@]}" apply -input=false -auto-approve -no-color

echo "── no generated secret in the state"
state="$("${tofu[@]}" show -json)"
leaks="$(jq -r '
  [.. | objects | select(has("address") and has("values")) |
   {address, v: .values} |
   select((.v.secret_string // "") != "" or (.v.password // "") != "" or (.v.result // "") != "") |
   .address] | .[]' <<<"$state")"
if [[ -n "$leaks" ]]; then
  echo "secret values found in the state:" >&2
  echo "$leaks" >&2
  exit 1
fi
echo "ok"

echo "── second plan is empty (bar known MiniStack gaps)"
# Attributes MiniStack does not round-trip (it does not store or echo them back): a diff on them
# is the emulator's, not the module's. Anything else in the second plan is a perpetual diff.
known_gaps='{
  "aws_vpc_security_group_egress_rule": ["description", "tags_all"],
  "aws_vpc_security_group_ingress_rule": ["description", "tags_all"],
  "aws_db_instance": ["enabled_cloudwatch_logs_exports"],
  "aws_autoscaling_group": ["default_instance_warmup"]
}'
plan_file="$example/sim.tfplan"
"${tofu[@]}" plan -input=false -no-color -out="$plan_file" >/dev/null
# Empty values ("", [], {}, null) compare equal, as they do in the plan OpenTofu prints.
drift="$("${tofu[@]}" show -json "$plan_file" | jq -r --argjson gaps "$known_gaps" '
  def norm: walk(if . == "" or . == [] or . == {} then null
                 elif type == "object" and ([.[]] | all(. == null)) then null
                 elif type == "array" and all(. == null) then null else . end);
  .resource_changes[]? | select(.change.actions != ["no-op"] and .change.actions != ["read"]) |
  if .change.actions != ["update"] then "\(.address): \(.change.actions | join("/"))"
  else
    . as $rc |
    [ ($rc.change.after // {}) | keys[] |
      select(($rc.change.before[.] | norm) != ($rc.change.after[.] | norm)) |
      select(. as $k | ($gaps[$rc.type] // []) | index($k) | not) ] as $attrs |
    if ($attrs | length) > 0 then "\($rc.address): \($attrs | join(", "))" else empty end
  end')"
rm -f "$plan_file"
if [[ -n "$drift" ]]; then
  echo "perpetual diff in the second plan:" >&2
  echo "$drift" >&2
  exit 1
fi
echo "ok"

if [[ "${KEEP:-0}" == "1" ]]; then
  echo "KEEP=1: left applied. Clean up with: tofu -chdir=$example destroy -auto-approve && task tf:sim:down"
  exit 0
fi

echo "── destroy (the guide's teardown: the master key is prevent_destroy, it leaves the state first)"
"${tofu[@]}" state rm -no-color \
  'module.stackd.module.secrets.aws_secretsmanager_secret_version.encryption_key' \
  'module.stackd.module.secrets.aws_secretsmanager_secret.encryption_key' >/dev/null
"${tofu[@]}" destroy -input=false -auto-approve -no-color
left="$("${tofu[@]}" state list | wc -l)"
[[ "$left" -eq 0 ]] || { echo "$left resources left in the state after destroy" >&2; exit 1; }
echo "ok"

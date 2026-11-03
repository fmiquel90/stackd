#!/usr/bin/env bash
# Runs the Stackd migration task (`alembic upgrade head` + base seed) on ECS and waits for it.
# Called by the module (terraform_data.migrate) when run_migrations = true, or by hand with the
# `migrate_command` output. Needs the AWS CLI v2 and these variables:
#   AWS_REGION CLUSTER TASK_DEFINITION SUBNETS (comma-separated) SECURITY_GROUP LOG_GROUP
set -euo pipefail

: "${AWS_REGION:?}" "${CLUSTER:?}" "${TASK_DEFINITION:?}" "${SUBNETS:?}" "${SECURITY_GROUP:?}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

network="awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=DISABLED}"
task_arn="$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$network" \
  --started-by stackd-migrate \
  --query 'tasks[0].taskArn' --output text)"

if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
  echo "run-migrations: the migration task did not start" >&2
  aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASK_DEFINITION" \
    --launch-type FARGATE --network-configuration "$network" --query 'failures' >&2 || true
  exit 1
fi
echo "run-migrations: started $task_arn"

# `aws ecs wait tasks-stopped` gives up after 10 minutes: poll instead (30 minutes at most).
for _ in $(seq 1 180); do
  status="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].lastStatus' --output text)"
  [[ "$status" == "STOPPED" ]] && break
  sleep 10
done

read -r exit_code reason < <(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
  --query 'tasks[0].[containers[0].exitCode, stoppedReason]' --output text)
if [[ "$status" != "STOPPED" || "$exit_code" != "0" ]]; then
  echo "run-migrations: FAILED (status=$status exit=$exit_code reason=$reason)." >&2
  echo "run-migrations: see the 'migrate/' streams of the ${LOG_GROUP:-API} log group." >&2
  exit 1
fi
echo "run-migrations: done"

# One-off migration task: `alembic upgrade head` then the base seed (default space + tier catalog),
# the image's `migrate` command. Same image, environment and secrets as the API (the production
# settings checks run there too), minus the Google client secret.
#
# With `run_migrations`, the machine running tofu/terraform starts it (bash + AWS CLI) each time
# the task definition changes — a new image digest — and waits for it to succeed, before the
# service rolls. Without, run the `migrate_command` output yourself before rolling the service.

resource "aws_ecs_task_definition" "migrate" {
  count = var.enabled ? 1 : 0

  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name        = "migrate"
    image       = var.image
    essential   = true
    command     = ["migrate"]
    environment = local.environment
    secrets     = local.migrate_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}

locals {
  migrate_env = var.enabled ? {
    AWS_REGION      = var.region
    CLUSTER         = aws_ecs_cluster.this.name
    TASK_DEFINITION = one(aws_ecs_task_definition.migrate[*].arn)
    SUBNETS         = join(",", var.subnet_ids)
    SECURITY_GROUP  = aws_security_group.api.id
    LOG_GROUP       = aws_cloudwatch_log_group.api.name
  } : {}
}

resource "terraform_data" "migrate" {
  count = var.enabled && var.run_migrations ? 1 : 0

  triggers_replace = [aws_ecs_task_definition.migrate[0].arn]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = "bash '${path.module}/../../scripts/run-migrations.sh'"
    environment = local.migrate_env
  }
}

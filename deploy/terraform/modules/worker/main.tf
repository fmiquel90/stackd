# Worker fleet: EC2 hosts in an Auto Scaling group, each running the agent container with the
# `docker` runner (SPECS §7.4) — the only runner a production API accepts, and one Fargate cannot
# offer (no Docker daemon there).
#
# Blast radius of a host:
# - IMDSv2 only, hop limit 1, and the instance metadata address dropped at the Docker bridge: job
#   containers (where repo code runs) cannot reach the instance role.
# - The instance role can pull the Stackd images, read the pool token, write the agent's logs and
#   complete its own termination hook — nothing else. No database, S3 or signing access: workers
#   reach states and saved plans through the API.
# - No inbound rule. Outbound: the API (internal ALB), git remotes, registries, cloud APIs.
#
# Fixed size (`worker_count`). Scale-in and rolling replacements (a new image digest or AMI →
# instance refresh) go through a termination lifecycle hook: the host drains its agent (SIGTERM →
# running jobs finish within `shutdown_timeout`) before the instance is terminated.

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "al2023" {
  count = var.ami_id == null ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${var.architecture}"
}

locals {
  asg_name  = "${var.name}-worker"
  hook_name = "drain"
  ami_id    = coalesce(var.ami_id, try(data.aws_ssm_parameter.al2023[0].insecure_value, null))

  # Registries to log in to: the ECR registries of the agent and runner images.
  images = concat([var.worker_image], values(var.runner_images))
  ecr_registries = distinct([
    for ref in local.images : split("/", ref)[0]
    if can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com$", split("/", ref)[0]))
  ])

  agent_env = merge(
    {
      STACKD_API_URL                  = var.api_url
      STACKD_RUNNER                   = "docker"
      STACKD_RUNNER_IMAGES            = jsonencode(var.runner_images)
      STACKD_POOL_TOKEN_FILE          = "/run/stackd/pool-token"
      STACKD_WORKSPACE_ROOT           = "/var/lib/stackd/ws"
      STACKD_MAX_CONCURRENT_JOBS      = tostring(var.max_concurrent_jobs)
      STACKD_SHUTDOWN_TIMEOUT_SECONDS = tostring(var.shutdown_timeout)
      STACKD_DOCKER_NETWORK           = "bridge"
    },
    var.docker_memory == null ? {} : { STACKD_DOCKER_MEMORY = var.docker_memory },
    var.docker_cpus == null ? {} : { STACKD_DOCKER_CPUS = var.docker_cpus },
  )

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region                = var.region
    log_group             = aws_cloudwatch_log_group.worker.name
    asg_name              = local.asg_name
    hook_name             = local.hook_name
    pool_token_secret_arn = var.pool_token_secret_arn
    worker_image          = var.worker_image
    images                = join(" ", local.images)
    ecr_registries        = join(" ", local.ecr_registries)
    agent_env             = join("\n", [for k, v in local.agent_env : "${k}=${v}"])
    # `docker stop` waits for the drain, systemd waits for `docker stop`.
    docker_stop_timeout  = var.shutdown_timeout + 60
    systemd_stop_timeout = var.shutdown_timeout + 120
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/stackd/${var.name}/worker"
  retention_in_days = var.log_retention_days
}

# ── Network ──────────────────────────────────────────────────────────────────────────────────

resource "aws_security_group" "worker" {
  name        = "${var.name}-worker"
  description = "Stackd worker hosts: no inbound"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "API, git remotes, registries, provider downloads, cloud APIs"
}

# ── Instance role ────────────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = var.ecr_repository_arns
  }
  statement {
    sid       = "PoolToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.pool_token_secret_arn]
  }
  statement {
    sid       = "PoolTokenDecrypt"
    actions   = ["kms:Decrypt"]
    resources = [var.data_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "AgentLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
  statement {
    sid     = "OwnTerminationHook"
    actions = ["autoscaling:CompleteLifecycleAction"]
    resources = [
      "arn:aws:autoscaling:${var.region}:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/${local.asg_name}",
    ]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker-host"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.enable_ssm ? 1 : 0
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.name}-worker-host"
  role = aws_iam_role.worker.name
}

# ── Hosts ────────────────────────────────────────────────────────────────────────────────────

resource "aws_launch_template" "worker" {
  name_prefix            = "${var.name}-worker-"
  image_id               = local.ami_id
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.worker.id]
  user_data              = base64encode(local.user_data)
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.worker.arn
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name}-worker" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${var.name}-worker" }
  }
}

resource "aws_autoscaling_group" "worker" {
  name                      = local.asg_name
  vpc_zone_identifier       = var.subnet_ids
  desired_capacity          = var.worker_count
  min_size                  = var.worker_count
  max_size                  = max(1, var.worker_count * 2) # room for the rolling replacement
  health_check_type         = "EC2"
  default_instance_warmup   = 120
  wait_for_capacity_timeout = "15m"

  launch_template {
    id      = aws_launch_template.worker.id
    version = aws_launch_template.worker.latest_version
  }

  # Created with the group, so the very first hosts are covered: a terminating host waits in
  # Terminating:Wait until its agent has drained (it completes the action itself), at most this.
  initial_lifecycle_hook {
    name                 = local.hook_name
    lifecycle_transition = "autoscaling:EC2_INSTANCE_TERMINATING"
    default_result       = "CONTINUE"
    heartbeat_timeout    = var.shutdown_timeout + 300
  }

  # A new launch template version (image digest, AMI, settings) replaces the hosts one batch at a
  # time, new ones first: capacity never drops, and old hosts drain through the hook.
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 100
      max_healthy_percentage = 200
      instance_warmup        = 120
    }
  }

  tag {
    key                 = "Name"
    value               = "${var.name}-worker"
    propagate_at_launch = true
  }

  depends_on = [aws_iam_role_policy.worker]
}

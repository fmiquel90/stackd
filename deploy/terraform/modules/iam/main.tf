# The API's ECS roles. The worker hosts have their own instance role (modules/worker): each side
# reads only its own secrets.

data "aws_region" "current" {}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ── Execution role: the ECS agent pulls the image, writes logs, injects the API's secrets ──────

resource "aws_iam_role" "api_execution" {
  name               = "${var.name}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "api_execution_base" {
  role       = aws_iam_role.api_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "api_execution_secrets" {
  statement {
    sid       = "ApiSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.api_secret_arns
  }
  statement {
    sid       = "DecryptThroughSecretsManager"
    actions   = ["kms:Decrypt"]
    resources = [var.data_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_execution_secrets" {
  name   = "api-secrets"
  role   = aws_iam_role.api_execution.id
  policy = data.aws_iam_policy_document.api_execution_secrets.json
}

# ── Task role: what the API process itself calls ─────────────────────────────────────────────

resource "aws_iam_role" "api_task" {
  name               = "${var.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_task" {
  # States, saved plans (SPECS §11). ListBucket: HeadBucket, and a 404 rather than a 403 on a miss.
  statement {
    sid       = "ArtifactsBucket"
    actions   = ["s3:ListBucket"]
    resources = [var.artifacts_bucket_arn]
  }
  statement {
    sid       = "ArtifactsObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.artifacts_bucket_arn}/*"]
  }
  # SSE-KMS of the artifacts bucket.
  statement {
    sid       = "ArtifactsEncryption"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.data_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
  # The workload OIDC issuer (SPECS §3.11): kms:Sign + kms:GetPublicKey, on that key only.
  statement {
    sid       = "OidcSigning"
    actions   = ["kms:Sign", "kms:GetPublicKey"]
    resources = [var.oidc_signing_key_arn]
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "api"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

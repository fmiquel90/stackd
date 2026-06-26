data "aws_caller_identity" "current" {}

# ── ECS Task Execution Role ────────────────────────────────────────────────────
# Used by the ECS agent to pull images and inject secrets. Both api and worker share this role.

resource "aws_iam_role" "execution" {
  name = "${var.name}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution_base" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.secret_arns
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn]
      },
    ]
  })
}

# ── API Task Role ──────────────────────────────────────────────────────────────
# Permissions the API process itself uses at runtime.

resource "aws_iam_role" "api" {
  name = "${var.name}-api-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "api_s3" {
  name = "s3-artifacts"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      Resource = [
        var.artifacts_bucket_arn,
        "${var.artifacts_bucket_arn}/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "api_kms" {
  name = "kms-decrypt"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
      Resource = [var.kms_key_arn]
    }]
  })
}

# ── Worker Task Role ───────────────────────────────────────────────────────────
# Workers call the API's HTTP state backend (not S3 directly) and use OIDC tokens
# from the API to assume customer-configured cloud roles via STS.

resource "aws_iam_role" "worker" {
  name = "${var.name}-worker-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Workers need to call STS AssumeRoleWithWebIdentity using the OIDC JWT the API mints.
# The actual target roles are customer-managed; this allows calling the STS endpoint.
resource "aws_iam_role_policy" "worker_sts" {
  name = "sts-assume-role"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sts:AssumeRoleWithWebIdentity"]
      Resource = "*"
    }]
  })
}

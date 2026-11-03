# Image repositories. Tags are IMMUTABLE and the module only ever runs images by digest
# (`<repo>@sha256:…`): what was reviewed is what runs.
#   api      api/Dockerfile
#   worker   worker/Dockerfile
#   runner   deploy/images/ (one tag per tool and version: opentofu-1.12.2, terraform-1.9.8)

locals {
  repositories = {
    api    = "${var.name}-api"
    worker = "${var.name}-worker"
    runner = "${var.name}-runner"
  }
}

resource "aws_ecr_repository" "this" {
  for_each = local.repositories

  name                 = each.value
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Drop untagged images (a digest still deployed should keep its tag)"
        selection    = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 30
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last ${var.image_retention_count} tagged images"
        selection    = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = var.image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })
}

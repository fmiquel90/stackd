# Two buckets:
#   artifacts  what the API stores (SPECS §11.3): states/{env}/{version}.tfstate (state history,
#              kept), artifacts/{run}/plan.tfplan (saved plans, AES-GCM encrypted by the API,
#              deleted when the run ends), logs/{run}/… (archived logs, not written yet). SSE-KMS,
#              versioning on, reachable by the API task role only.
#   spa        the built front (front/dist), read by CloudFront through Origin Access Control.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "spa" {
  bucket = "${var.name}-spa-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "spa" {
  bucket = aws_s3_bucket.spa.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "spa" {
  bucket                  = aws_s3_bucket.spa.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_ownership_controls" "spa" {
  bucket = aws_s3_bucket.spa.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  # Saved plans hold the run's variable values (encrypted): a deleted one must not linger as a
  # noncurrent version, and a forgotten one (a run never confirmed) goes after the retention.
  rule {
    id     = "saved-plans"
    status = "Enabled"
    filter {
      prefix = "artifacts/"
    }
    expiration {
      days = var.saved_plan_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  # State versions are distinct objects (one key per version) and are kept; this only bounds the
  # copies an overwrite or a delete leaves behind.
  rule {
    id     = "states-noncurrent"
    status = "Enabled"
    filter {
      prefix = "states/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  # Archived run logs (SPECS §5.2: one year), once the API writes them.
  rule {
    id     = "logs"
    status = "Enabled"
    filter {
      prefix = "logs/"
    }
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

data "aws_iam_policy_document" "artifacts" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifacts.json

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

# Origin Access Control: created here, used by the distribution and the SPA bucket policy (root).
resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "${var.name}-spa"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Symmetric key: encryption at rest of RDS, the artifacts bucket (SSE-KMS) and the secrets.
resource "aws_kms_key" "data" {
  description             = "${var.name}: data at rest (RDS, S3 artifacts, Secrets Manager)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# Asymmetric key of the workload OIDC issuer (SPECS §3.11): the API signs every workload token
# with kms:Sign (RSASSA_PKCS1_V1_5_SHA_256 = RS256); the private key never leaves KMS. A production
# API refuses to start without it (STACKD_OIDC_SIGNER=kms). Asymmetric keys do not auto-rotate:
# rotate by creating a new key and pointing STACKD_OIDC_KMS_KEY_ARN at it — the issuer keeps the
# old public key in its JWKS until the tokens it signed have expired.
resource "aws_kms_key" "oidc_signing" {
  description              = "${var.name}: workload OIDC issuer signing key (RS256)"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = 30
  enable_key_rotation      = false
}

resource "aws_kms_alias" "oidc_signing" {
  name          = "alias/${var.name}-oidc-signing"
  target_key_id = aws_kms_key.oidc_signing.key_id
}

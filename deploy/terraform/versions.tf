terraform {
  # Write-only arguments (`*_wo`) and ephemeral resources keep the generated secrets out of the
  # state and the plan: OpenTofu >= 1.11 or Terraform >= 1.11.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.50 fixes `secret_string_wo_version` referencing a resource created in the same apply.
      version = "~> 6.50"
      # The caller configures the providers (see examples/): this module declares none. The alias
      # is the us-east-1 provider CloudFront certificates must be created with.
      configuration_aliases = [aws.us_east_1]
    }
    random = {
      source = "hashicorp/random"
      # 3.9 adds the ephemeral `random_bytes` (the master encryption key).
      version = "~> 3.9"
    }
  }
}

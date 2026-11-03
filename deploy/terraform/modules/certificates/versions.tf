terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 6.50"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

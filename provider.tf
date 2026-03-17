# ─────────────────────────────────────────────────────────────────────────────
# provider.tf
#
# The AWS provider's `default_tags` block applies the specified tags to EVERY
# taggable resource managed by this provider — no changes needed to individual
# resource blocks in existing code.
#
# How it works:
#   - Tags defined here are merged onto every resource automatically.
#   - If a resource already defines the same tag key, the resource-level value
#     wins (resource tags take precedence over default_tags).
#   - Works for all taggable AWS resources: EC2, RDS, S3, Lambda, ECS, etc.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"   # default_tags has been stable since v4
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      # ── Cleanup tag (picked up by the nightly Lambda) ──────────────────────
      auto-delete = "true"
    }
  }
}

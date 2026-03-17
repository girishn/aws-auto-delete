# ─────────────────────────────────────────────────────────────────────────────
# variables.tf
# ─────────────────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-southeast-2"   # Sydney — change to your region
}

variable "environment" {
  description = "Deployment environment (used in default_tags)"
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Team or individual who owns these resources (used in default_tags)"
  type        = string
  default     = "platform-team"
}

variable "cleanup_schedule" {
  description = <<-EOT
    EventBridge cron expression for the nightly cleanup.
    All times are UTC. Examples:
      cron(0 20 * * ? *)       — 8 PM UTC daily (default)
      cron(0 14 * * ? *)       — 2 PM UTC = midnight AEST (Sydney)
      cron(0 20 ? * MON-FRI *) — weeknights only
  EOT
  type    = string
  default = "cron(45 0 * * ? *)"   # 10:45 PM AEST (UTC+10)
}

variable "notification_email" {
  description = "Optional email address for cleanup summary notifications. Leave empty to skip."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "How many days to retain Lambda CloudWatch logs"
  type        = number
  default     = 30
}

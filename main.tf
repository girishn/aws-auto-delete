# ─────────────────────────────────────────────────────────────────────────────
# main.tf  —  Lambda, CloudWatch Logs, EventBridge Scheduler, SNS (optional)
# ─────────────────────────────────────────────────────────────────────────────

# ── Package lambda_cleanup.py into a zip ──────────────────────────────────────
# Terraform's archive_file data source creates the zip locally at plan time.
# The source file must live alongside your .tf files.

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_cleanup.py"
  output_path = "${path.module}/.build/lambda_cleanup.zip"
}

# ── Lambda Function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "cleanup" {
  function_name    = "nightly-resource-cleanup"
  description      = "Deletes all resources tagged auto-delete=true"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "lambda_cleanup.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.cleanup_lambda.arn
  timeout          = 300   # 5 minutes
  memory_size      = 128   # minimum — this job is I/O bound, not CPU bound

  environment {
    variables = {
      SNS_TOPIC_ARN = var.notification_email != "" ? aws_sns_topic.cleanup_notifications[0].arn : ""
    }
  }

  # Override the global auto-delete tag — keep this function around!
  tags = {
    auto-delete = "false"
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_cloudwatch_log_group.cleanup_lambda,
  ]
}

# ── CloudWatch Log Group ──────────────────────────────────────────────────────
# Explicitly managed so we can set retention. Without this, Lambda creates
# the log group automatically with no expiry (logs accumulate forever).

resource "aws_cloudwatch_log_group" "cleanup_lambda" {
  name              = "/aws/lambda/nightly-resource-cleanup"
  retention_in_days = var.log_retention_days

  lifecycle {
    ignore_changes = [tags_all]
  }

  tags = {
    auto-delete = "false"
  }
}

# ── EventBridge Scheduler ─────────────────────────────────────────────────────

resource "aws_scheduler_schedule" "nightly_cleanup" {
  name        = "nightly-resource-cleanup"
  description = "Triggers the nightly resource cleanup Lambda"

  schedule_expression          = var.cleanup_schedule
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.cleanup.arn
    role_arn = aws_iam_role.cleanup_scheduler.arn
  }
}

# ── SNS Notification (optional) ───────────────────────────────────────────────
# Only created when notification_email variable is non-empty.

resource "aws_sns_topic" "cleanup_notifications" {
  count = var.notification_email != "" ? 1 : 0
  name  = "nightly-cleanup-notifications"

  lifecycle {
    ignore_changes = [tags_all]
  }

  tags = {
    auto-delete = "false"
  }
}

resource "aws_sns_topic_subscription" "cleanup_email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.cleanup_notifications[0].arn
  protocol  = "email"
  endpoint  = var.notification_email
}

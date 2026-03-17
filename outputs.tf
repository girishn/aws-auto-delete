# ─────────────────────────────────────────────────────────────────────────────
# outputs.tf
# ─────────────────────────────────────────────────────────────────────────────

output "lambda_function_name" {
  description = "Name of the cleanup Lambda function"
  value       = aws_lambda_function.cleanup.function_name
}

output "lambda_function_arn" {
  description = "ARN of the cleanup Lambda function"
  value       = aws_lambda_function.cleanup.arn
}

output "scheduler_name" {
  description = "EventBridge schedule name"
  value       = aws_scheduler_schedule.nightly_cleanup.name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for Lambda output"
  value       = aws_cloudwatch_log_group.cleanup_lambda.name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for cleanup notifications (empty if not configured)"
  value       = var.notification_email != "" ? aws_sns_topic.cleanup_notifications[0].arn : "not configured"
}

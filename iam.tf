# ─────────────────────────────────────────────────────────────────────────────
# iam.tf  —  IAM role + least-privilege policy for the cleanup Lambda
# ─────────────────────────────────────────────────────────────────────────────

# ── Trust policy: only Lambda can assume this role ────────────────────────────

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cleanup_lambda" {
  name               = "nightly-cleanup-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = {
    # Override the global auto-delete tag — we want this role to persist
    auto-delete = "false"
  }
}

# ── Basic Lambda execution (CloudWatch Logs) ──────────────────────────────────

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.cleanup_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ── Cleanup permissions: one statement per service ────────────────────────────

data "aws_iam_policy_document" "cleanup_permissions" {

  # Resource Groups Tagging API — list all tagged resources
  statement {
    sid     = "TaggingAPI"
    effect  = "Allow"
    actions = ["tag:GetResources"]
    resources = ["*"]
  }

  # EC2
  statement {
    sid    = "EC2Cleanup"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:TerminateInstances",
      "ec2:DeleteVolume",
      "ec2:DeleteSnapshot",
      "ec2:DeregisterImage",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteKeyPair",
    ]
    resources = ["*"]
  }

  # RDS + Aurora (instances, clusters, snapshots)
  statement {
    sid    = "RDSCleanup"
    effect = "Allow"
    actions = [
      "rds:DescribeDBInstances",
      "rds:DescribeDBClusters",
      "rds:DeleteDBInstance",
      "rds:DeleteDBCluster",
      "rds:DeleteDBSnapshot",
      "rds:DeleteDBClusterSnapshot",
      "rds:DescribeDBSnapshots",
      "rds:DescribeDBClusterSnapshots",
    ]
    resources = ["*"]
  }

  # Amazon Bedrock — custom models & provisioned throughput
  statement {
    sid    = "BedrockCleanup"
    effect = "Allow"
    actions = [
      "bedrock:DeleteCustomModel",
      "bedrock:DeleteProvisionedModelThroughput",
      "bedrock:GetCustomModel",
      "bedrock:GetProvisionedModelThroughput",
      "bedrock:ListCustomModels",
    ]
    resources = ["*"]
  }

  # AWS App Runner — services & VPC connectors
  statement {
    sid    = "AppRunnerCleanup"
    effect = "Allow"
    actions = [
      "apprunner:DeleteService",
      "apprunner:DeleteVpcConnector",
      "apprunner:DescribeService",
      "apprunner:ListServices",
    ]
    resources = ["*"]
  }

  # Amazon SageMaker — endpoints, models, notebooks, pipelines, domains, jobs
  statement {
    sid    = "SageMakerCleanup"
    effect = "Allow"
    actions = [
      "sagemaker:DeleteEndpoint",
      "sagemaker:DeleteEndpointConfig",
      "sagemaker:DeleteModel",
      "sagemaker:DeleteNotebookInstance",
      "sagemaker:StopNotebookInstance",
      "sagemaker:DeletePipeline",
      "sagemaker:DeleteDomain",
      "sagemaker:DeleteFeatureGroup",
      "sagemaker:StopTrainingJob",
      "sagemaker:StopProcessingJob",
      "sagemaker:StopCompilationJob",
      "sagemaker:StopHyperParameterTuningJob",
      "sagemaker:DescribeNotebookInstance",
      "sagemaker:DescribeDomain",
      "sagemaker:ListUserProfiles",
      "sagemaker:ListApps",
    ]
    resources = ["*"]
  }

  # ECS
  statement {
    sid    = "ECSCleanup"
    effect = "Allow"
    actions = [
      "ecs:ListServices",
      "ecs:DescribeClusters",
      "ecs:UpdateService",
      "ecs:DeleteService",
      "ecs:DeleteCluster",
    ]
    resources = ["*"]
  }

  # ELBv2
  statement {
    sid    = "ELBCleanup"
    effect = "Allow"
    actions = [
      "elasticloadbalancing:DeleteLoadBalancer",
      "elasticloadbalancing:DeleteTargetGroup",
    ]
    resources = ["*"]
  }

  # Lambda (delete other functions, not itself)
  statement {
    sid       = "LambdaCleanup"
    effect    = "Allow"
    actions   = ["lambda:DeleteFunction"]
    resources = ["*"]
  }

  # SQS
  statement {
    sid    = "SQSCleanup"
    effect = "Allow"
    actions = [
      "sqs:GetQueueUrl",
      "sqs:DeleteQueue",
    ]
    resources = ["*"]
  }

  # SNS
  statement {
    sid       = "SNSCleanup"
    effect    = "Allow"
    actions   = ["sns:DeleteTopic"]
    resources = ["*"]
  }

  # DynamoDB
  statement {
    sid       = "DynamoCleanup"
    effect    = "Allow"
    actions   = ["dynamodb:DeleteTable"]
    resources = ["*"]
  }

  # S3 — paginated empty (versions + delete markers) then delete bucket
  statement {
    sid    = "S3Cleanup"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:ListObjectsV2",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:DeleteBucket",
    ]
    resources = ["*"]
  }

  # S3 Vectors — indexes inside vector buckets, then vector buckets
  statement {
    sid    = "S3VectorsCleanup"
    effect = "Allow"
    actions = [
      "s3vectors:ListIndexes",
      "s3vectors:DeleteIndex",
      "s3vectors:DeleteVectorBucket",
      "s3vectors:GetVectorBucket",
    ]
    resources = ["*"]
  }

  # CloudFormation
  statement {
    sid    = "CFNCleanup"
    effect = "Allow"
    actions = [
      "cloudformation:DescribeStacks",
      "cloudformation:DeleteStack",
    ]
    resources = ["*"]
  }

  # OpenSearch Service — managed domains (es) + Serverless (aoss)
  statement {
    sid    = "OpenSearchCleanup"
    effect = "Allow"
    actions = [
      "es:DeleteDomain",
      "es:DescribeDomain",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "OpenSearchServerlessCleanup"
    effect = "Allow"
    actions = [
      "aoss:DeleteCollection",
      "aoss:BatchGetCollection",
    ]
    resources = ["*"]
  }

  # OpenSearch Ingestion (OSIS) pipelines
  statement {
    sid    = "OpenSearchIngestionCleanup"
    effect = "Allow"
    actions = [
      "osis:DeletePipeline",
      "osis:GetPipeline",
    ]
    resources = ["*"]
  }

  # SNS publish — only needed if notification email is configured
  dynamic "statement" {
    for_each = var.notification_email != "" ? [1] : []
    content {
      sid       = "SNSPublishNotification"
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [aws_sns_topic.cleanup_notifications[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "cleanup_permissions" {
  name   = "cleanup-permissions"
  role   = aws_iam_role.cleanup_lambda.id
  policy = data.aws_iam_policy_document.cleanup_permissions.json
}

# ── Trust policy: EventBridge Scheduler → Lambda ──────────────────────────────

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cleanup_scheduler" {
  name               = "nightly-cleanup-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json

  tags = {
    auto-delete = "false"
  }
}

data "aws_iam_policy_document" "scheduler_invoke_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.cleanup.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name   = "invoke-cleanup-lambda"
  role   = aws_iam_role.cleanup_scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke_lambda.json
}

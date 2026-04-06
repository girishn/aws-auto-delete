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
      "ec2:DescribeRegions",
      "ec2:DescribeInstances",
      "ec2:TerminateInstances",
      "ec2:DeleteVolume",
      "ec2:DeleteSnapshot",
      "ec2:DeregisterImage",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteKeyPair",
      "ec2:DeleteNatGateway",
      "ec2:DescribeInternetGateways",
      "ec2:DetachInternetGateway",
      "ec2:DeleteInternetGateway",
      "ec2:DescribeAddresses",
      "ec2:DisassociateAddress",
      "ec2:ReleaseAddress",
      "ec2:DeleteVpcEndpoints",
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
      "rds:DeleteDBProxy",
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

  # SageMaker ML Lineage Tracking — auto-created alongside endpoint deployments
  # context, action, artifact and association are created automatically when
  # endpoints are deployed and inherit the endpoint's tags
  statement {
    sid    = "SageMakerLineageCleanup"
    effect = "Allow"
    actions = [
      "sagemaker:DeleteContext",
      "sagemaker:DeleteAction",
      "sagemaker:DeleteArtifact",
      "sagemaker:DeleteAssociation",
      "sagemaker:ListAssociations",
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

  # EKS — control plane + managed node groups + Fargate
  statement {
    sid    = "EKSCleanup"
    effect = "Allow"
    actions = [
      "eks:DeleteCluster",
      "eks:ListNodegroups",
      "eks:DeleteNodegroup",
      "eks:ListFargateProfiles",
      "eks:DeleteFargateProfile",
    ]
    resources = ["*"]
  }

  # ElastiCache — replication groups, clusters, serverless
  statement {
    sid    = "ElastiCacheCleanup"
    effect = "Allow"
    actions = [
      "elasticache:DeleteReplicationGroup",
      "elasticache:DeleteCacheCluster",
      "elasticache:DeleteServerlessCache",
    ]
    resources = ["*"]
  }

  # EFS
  statement {
    sid    = "EFSCleanup"
    effect = "Allow"
    actions = [
      "elasticfilesystem:DescribeMountTargets",
      "elasticfilesystem:DeleteMountTarget",
      "elasticfilesystem:DeleteFileSystem",
    ]
    resources = ["*"]
  }

  # Redshift provisioned + Serverless
  statement {
    sid    = "RedshiftCleanup"
    effect = "Allow"
    actions = [
      "redshift:DeleteCluster",
      "redshift-serverless:DeleteWorkgroup",
      "redshift-serverless:DeleteNamespace",
    ]
    resources = ["*"]
  }

  # MemoryDB
  statement {
    sid    = "MemoryDBCleanup"
    effect = "Allow"
    actions = ["memorydb:DeleteCluster"]
    resources = ["*"]
  }

  # MSK
  statement {
    sid    = "MSKCleanup"
    effect = "Allow"
    actions = ["kafka:DeleteCluster"]
    resources = ["*"]
  }

  # Kinesis Data Streams + Firehose
  statement {
    sid    = "KinesisCleanup"
    effect = "Allow"
    actions = [
      "kinesis:DeleteStream",
      "firehose:DeleteDeliveryStream",
    ]
    resources = ["*"]
  }

  # Step Functions
  statement {
    sid    = "StepFunctionsCleanup"
    effect = "Allow"
    actions = ["states:DeleteStateMachine"]
    resources = ["*"]
  }

  # EventBridge rules
  statement {
    sid    = "EventBridgeCleanup"
    effect = "Allow"
    actions = [
      "events:ListTargetsByRule",
      "events:RemoveTargets",
      "events:DeleteRule",
    ]
    resources = ["*"]
  }

  # ECR
  statement {
    sid    = "ECRCleanup"
    effect = "Allow"
    actions = ["ecr:DeleteRepository"]
    resources = ["*"]
  }

  # Secrets Manager
  statement {
    sid    = "SecretsManagerCleanup"
    effect = "Allow"
    actions = ["secretsmanager:DeleteSecret"]
    resources = ["*"]
  }

  # Glue crawlers + jobs
  statement {
    sid    = "GlueCleanup"
    effect = "Allow"
    actions = [
      "glue:DeleteCrawler",
      "glue:DeleteJob",
    ]
    resources = ["*"]
  }

  # EMR
  statement {
    sid    = "EMRCleanup"
    effect = "Allow"
    actions = ["elasticmapreduce:TerminateJobFlows"]
    resources = ["*"]
  }

  # Amplify
  statement {
    sid    = "AmplifyCleanup"
    effect = "Allow"
    actions = ["amplify:DeleteApp"]
    resources = ["*"]
  }

  # Amazon MQ
  statement {
    sid    = "MQCleanup"
    effect = "Allow"
    actions = ["mq:DeleteBroker"]
    resources = ["*"]
  }

  # DMS replication instances
  statement {
    sid    = "DMSCleanup"
    effect = "Allow"
    actions = ["dms:DeleteReplicationInstance"]
    resources = ["*"]
  }

  # Batch compute environments
  statement {
    sid    = "BatchCleanup"
    effect = "Allow"
    actions = [
      "batch:UpdateComputeEnvironment",
      "batch:DeleteComputeEnvironment",
    ]
    resources = ["*"]
  }

  # API Gateway REST (v1) + HTTP/WebSocket (v2)
  statement {
    sid    = "APIGatewayCleanup"
    effect = "Allow"
    actions = [
      "apigateway:DeleteRestApi",
      "apigatewayv2:DeleteApi",
    ]
    resources = ["*"]
  }

  # AppSync
  statement {
    sid    = "AppSyncCleanup"
    effect = "Allow"
    actions = ["appsync:DeleteGraphqlApi"]
    resources = ["*"]
  }

  # CloudWatch Logs
  statement {
    sid    = "CloudWatchLogsCleanup"
    effect = "Allow"
    actions = ["logs:DeleteLogGroup"]
    resources = ["*"]
  }

  # DocumentDB Elastic
  statement {
    sid    = "DocDBElasticCleanup"
    effect = "Allow"
    actions = ["docdb-elastic:DeleteCluster"]
    resources = ["*"]
  }

  # Timestream
  statement {
    sid    = "TimestreamCleanup"
    effect = "Allow"
    actions = [
      "timestream:ListTables",
      "timestream:DeleteTable",
      "timestream:DeleteDatabase",
    ]
    resources = ["*"]
  }

  # Global Accelerator (control-plane API)
  statement {
    sid    = "GlobalAcceleratorCleanup"
    effect = "Allow"
    actions = ["globalaccelerator:DeleteAccelerator"]
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

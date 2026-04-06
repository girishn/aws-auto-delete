"""
Nightly AWS Resource Cleanup Lambda
------------------------------------
Finds all resources tagged with auto-delete=true and deletes them.
Triggered by EventBridge Scheduler (e.g. cron at 8pm UTC nightly).

Supported resource types:
  - EC2 instances, key pairs, security groups, EBS volumes, snapshots, AMIs,
    NAT gateways, internet gateways, Elastic IPs, VPC interface endpoints
  - RDS instances, clusters & Aurora clusters, RDS Proxy
  - ECS clusters & services
  - ELBv2 load balancers & target groups
  - Lambda functions
  - SQS queues
  - SNS topics
  - DynamoDB tables
  - S3 buckets (empties then deletes)
  - CloudFormation stacks
  - Amazon Bedrock  — custom models, model import jobs, provisioned throughput
  - AWS App Runner  — services & VPC connectors
  - Amazon SageMaker — endpoints, endpoint configs, models, notebook instances,
                       pipelines, domains, feature groups, training/processing jobs
  - Billable “running” infrastructure — EKS, ElastiCache, EFS, Redshift &
    Redshift Serverless, MemoryDB, MSK, Kinesis & Firehose, Step Functions,
    EventBridge rules, ECR repos, Secrets Manager secrets, Glue crawlers/jobs,
    EMR clusters, Amplify apps, MQ brokers, DMS replication instances, Batch
    compute environments, API Gateway (REST v1 / HTTP+WS v2), AppSync APIs,
    CloudWatch Logs groups, DocumentDB Elastic, Timestream databases,
    Global Accelerator accelerators
"""

import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TAG_KEY   = "auto-delete"
TAG_VALUE = "true"


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_all_tagged_resources(session):
    """Return every resource ARN tagged auto-delete=true across all regions."""
    ec2 = session.client("ec2")
    try:
        regions = [r["RegionName"] for r in ec2.describe_regions()["Regions"]]
    except ClientError as e:
        logger.error(f"Could not list regions, falling back to Lambda region: {e}")
        regions = [session.region_name or "us-east-1"]

    arns = []
    for region in regions:
        try:
            client = session.client("resourcegroupstaggingapi", region_name=region)
            paginator = client.get_paginator("get_resources")
            for page in paginator.paginate(
                TagFilters=[{"Key": TAG_KEY, "Values": [TAG_VALUE]}]
            ):
                arns.extend(r["ResourceARN"] for r in page["ResourceTagMappingList"])
        except ClientError as e:
            logger.warning(f"Tagging API in {region} failed (skipping): {e}")
    return arns


def service_of(arn: str) -> str:
    """Extract the AWS service name from an ARN."""
    # arn:aws:<service>:<region>:<account>:<type>/<id>
    parts = arn.split(":")
    return parts[2] if len(parts) > 2 else ""


def resource_type_of(arn: str) -> str:
    """Extract resource type (e.g. 'instance', 'db') from an ARN."""
    parts = arn.split(":")
    if len(parts) > 5:
        resource = parts[5]          # e.g. "instance/i-0abc123"
        return resource.split("/")[0]
    return ""


def region_of(arn: str) -> str | None:
    """Region segment of a regional ARN; None for global ARNs (e.g. S3, IAM)."""
    parts = arn.split(":")
    if len(parts) > 3 and parts[3]:
        return parts[3]
    return None


# ── Per-service deletors ──────────────────────────────────────────────────────

def delete_ec2(session, arn: str):
    ec2 = session.client("ec2")
    rtype = resource_type_of(arn)
    rid   = arn.split("/")[-1]

    if rtype == "instance":
        ec2.terminate_instances(InstanceIds=[rid])
        logger.info(f"Terminated EC2 instance: {rid}")

    elif rtype == "volume":
        ec2.delete_volume(VolumeId=rid)
        logger.info(f"Deleted EBS volume: {rid}")

    elif rtype == "snapshot":
        ec2.delete_snapshot(SnapshotId=rid)
        logger.info(f"Deleted snapshot: {rid}")

    elif rtype == "image":
        ec2.deregister_image(ImageId=rid)
        logger.info(f"Deregistered AMI: {rid}")

    elif rtype == "security-group":
        ec2.delete_security_group(GroupId=rid)
        logger.info(f"Deleted security group: {rid}")

    elif rtype == "key-pair":
        ec2.delete_key_pair(KeyPairId=rid)
        logger.info(f"Deleted key pair: {rid}")

    elif rtype == "natgateway":
        ec2.delete_nat_gateway(NatGatewayId=rid)
        logger.info(f"Deleted NAT gateway: {rid}")

    elif rtype == "internet-gateway":
        resp = ec2.describe_internet_gateways(InternetGatewayIds=[rid])
        igws = resp.get("InternetGateways", [])
        if not igws:
            logger.warning(f"Internet gateway {rid} not found — skipping")
            return
        for att in igws[0].get("Attachments", []):
            vpc_id = att.get("VpcId")
            if vpc_id:
                ec2.detach_internet_gateway(
                    InternetGatewayId=rid, VpcId=vpc_id
                )
                logger.info(f"Detached internet gateway {rid} from VPC {vpc_id}")
        ec2.delete_internet_gateway(InternetGatewayId=rid)
        logger.info(f"Deleted internet gateway: {rid}")

    elif rtype == "elastic-ip":
        resp = ec2.describe_addresses(AllocationIds=[rid])
        addrs = resp.get("Addresses", [])
        if not addrs:
            logger.warning(f"Elastic IP {rid} not found — skipping")
            return
        assoc = addrs[0].get("AssociationId")
        if assoc:
            ec2.disassociate_address(AssociationId=assoc)
            logger.info(f"Disassociated Elastic IP {rid} before release")
        ec2.release_address(AllocationId=rid)
        logger.info(f"Released Elastic IP: {rid}")

    elif rtype == "vpc-endpoint":
        ec2.delete_vpc_endpoints(VpcEndpointIds=[rid])
        logger.info(f"Deleted VPC endpoint: {rid}")

    else:
        logger.warning(f"Unknown EC2 resource type '{rtype}' — skipping {arn}")


def delete_rds(session, arn: str):
    """
    Handles RDS instances, Multi-AZ clusters, and Aurora clusters.

    Aurora ARN formats:
      cluster:  arn:aws:rds:<region>:<acct>:cluster:<id>
      instance: arn:aws:rds:<region>:<acct>:db:<id>

    Aurora instances must be deleted before their cluster. When the Tagging
    API returns both, we process instances first by sorting (db < cluster
    alphabetically). Alternatively, tag only the cluster — Aurora will
    delete its instances automatically when the cluster is deleted.
    """
    rds   = session.client("rds")
    rtype = resource_type_of(arn)
    rid   = arn.split(":")[-1]

    if rtype == "db":
        rds.delete_db_instance(
            DBInstanceIdentifier=rid,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )
        logger.info(f"Deleted RDS/Aurora instance: {rid}")

    elif rtype == "cluster":
        # Covers both Aurora and RDS Multi-AZ clusters
        rds.delete_db_cluster(
            DBClusterIdentifier=rid,
            SkipFinalSnapshot=True,
        )
        logger.info(f"Deleted RDS/Aurora cluster: {rid}")

    elif rtype == "cluster-snapshot":
        rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=rid)
        logger.info(f"Deleted Aurora cluster snapshot: {rid}")

    elif rtype == "snapshot":
        rds.delete_db_snapshot(DBSnapshotIdentifier=rid)
        logger.info(f"Deleted RDS snapshot: {rid}")

    elif rtype == "db-proxy":
        rds.delete_db_proxy(DBProxyName=rid)
        logger.info(f"Deleted RDS Proxy: {rid}")

    else:
        logger.warning(f"Unknown RDS resource type '{rtype}' — skipping {arn}")


def delete_ecs(session, arn: str):
    ecs   = session.client("ecs")
    rtype = resource_type_of(arn)

    if rtype == "cluster":
        cluster = arn.split("/")[-1]
        # Drain all services first
        services = ecs.list_services(cluster=cluster).get("serviceArns", [])
        if services:
            ecs.delete_services(cluster=cluster, services=services, force=True)
            logger.info(f"Deleted ECS services in cluster: {cluster}")
        ecs.delete_cluster(cluster=cluster)
        logger.info(f"Deleted ECS cluster: {cluster}")

    elif rtype == "service":
        parts   = arn.split("/")
        cluster = parts[-2]
        service = parts[-1]
        ecs.update_service(cluster=cluster, service=service, desiredCount=0)
        ecs.delete_service(cluster=cluster, service=service, force=True)
        logger.info(f"Deleted ECS service: {service}")

    else:
        logger.warning(f"Unknown ECS resource type '{rtype}' — skipping {arn}")


def delete_elasticloadbalancing(session, arn: str):
    elb   = session.client("elbv2")
    rtype = resource_type_of(arn)

    if rtype in ("loadbalancer", "app", "net"):
        elb.delete_load_balancer(LoadBalancerArn=arn)
        logger.info(f"Deleted load balancer: {arn}")

    elif rtype == "targetgroup":
        elb.delete_target_group(TargetGroupArn=arn)
        logger.info(f"Deleted target group: {arn}")

    else:
        logger.warning(f"Unknown ELB resource type '{rtype}' — skipping {arn}")


def delete_lambda_fn(session, arn: str):
    fn_name = arn.split(":")[-1]
    # Don't delete ourselves!
    if fn_name == os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        logger.warning("Skipping self-deletion of this Lambda function.")
        return
    lmb = session.client("lambda")
    lmb.delete_function(FunctionName=fn_name)
    logger.info(f"Deleted Lambda function: {fn_name}")


def delete_sqs(session, arn: str):
    sqs      = session.client("sqs")
    parts    = arn.split(":")
    account  = parts[4]
    name     = parts[5]
    region   = session.region_name
    url      = f"https://sqs.{region}.amazonaws.com/{account}/{name}"
    sqs.delete_queue(QueueUrl=url)
    logger.info(f"Deleted SQS queue: {name}")


def delete_sns(session, arn: str):
    sns = session.client("sns")
    sns.delete_topic(TopicArn=arn)
    logger.info(f"Deleted SNS topic: {arn}")


def delete_dynamodb(session, arn: str):
    table_name = arn.split("/")[-1]
    ddb = session.client("dynamodb")
    ddb.delete_table(TableName=table_name)
    logger.info(f"Deleted DynamoDB table: {table_name}")


def empty_and_delete_s3(session, arn: str):
    """
    Empties and deletes an S3 bucket.

    Handles all three cases that can block bucket deletion:
      1. Regular objects                  — deleted in chunks of 1000
      2. All versions of versioned objects — deleted in chunks of 1000
      3. Delete markers (versioned buckets leave these behind)

    Uses the low-level client with paginated list calls + batch delete
    (up to 1000 objects per DeleteObjects request) for efficiency on
    large buckets. The high-level resource .objects.all().delete() can
    silently miss delete markers, so we use the explicit paginator approach.
    """
    bucket = arn.split(":::")[-1]
    s3     = session.client("s3")

    # ── Step 1: delete all object versions and delete markers ─────────────────
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects_to_delete = []

        for version in page.get("Versions", []):
            objects_to_delete.append(
                {"Key": version["Key"], "VersionId": version["VersionId"]}
            )
        for marker in page.get("DeleteMarkers", []):
            objects_to_delete.append(
                {"Key": marker["Key"], "VersionId": marker["VersionId"]}
            )

        if objects_to_delete:
            # DeleteObjects accepts max 1000 per request
            for chunk in _chunks(objects_to_delete, 1000):
                s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": chunk, "Quiet": True},
                )
            logger.info(f"Deleted {len(objects_to_delete)} versions/markers from {bucket}")

    # ── Step 2: delete any remaining unversioned objects ─────────────────────
    # (covers buckets that never had versioning enabled)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects_to_delete = [
            {"Key": obj["Key"]} for obj in page.get("Contents", [])
        ]
        if objects_to_delete:
            for chunk in _chunks(objects_to_delete, 1000):
                s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": chunk, "Quiet": True},
                )
            logger.info(f"Deleted {len(objects_to_delete)} objects from {bucket}")

    # ── Step 3: delete the now-empty bucket ───────────────────────────────────
    s3.delete_bucket(Bucket=bucket)
    logger.info(f"Deleted S3 bucket: {bucket}")


def _chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def delete_s3vectors(session, arn: str):
    """
    Handles Amazon S3 Vectors resources.

    S3 Vectors (launched 2025) introduces two taggable resource types:
      - vector-bucket-index  arn:aws:s3vectors:<r>:<a>:vector-bucket-index/<bucket>/<index>
      - vector-bucket        arn:aws:s3vectors:<r>:<a>:vector-bucket/<name>

    Deletion order matters: all indexes inside a vector bucket must be
    deleted before the bucket itself can be removed.

    If only the vector-bucket ARN is tagged (not individual indexes),
    this handler lists and deletes all indexes first automatically.
    """
    s3v = session.client("s3vectors")

    # resource section: e.g. "vector-bucket-index/my-bucket/my-index"
    #                    or  "vector-bucket/my-bucket"
    resource_section = ":".join(arn.split(":")[5:])
    rtype = resource_section.split("/")[0]

    if rtype == "vector-bucket-index":
        parts        = resource_section.split("/")
        vector_bucket = parts[1]
        index_name   = parts[2]
        s3v.delete_index(
            vectorBucketName=vector_bucket,
            indexName=index_name,
        )
        logger.info(f"Deleted S3 Vectors index: {index_name} in bucket {vector_bucket}")

    elif rtype == "vector-bucket":
        vector_bucket = resource_section.split("/", 1)[1]

        # Delete all indexes first — bucket deletion will fail otherwise
        paginator = s3v.get_paginator("list_indexes")
        index_count = 0
        for page in paginator.paginate(vectorBucketName=vector_bucket):
            for index in page.get("indexes", []):
                s3v.delete_index(
                    vectorBucketName=vector_bucket,
                    indexName=index["indexName"],
                )
                index_count += 1
        if index_count:
            logger.info(f"Deleted {index_count} indexes from S3 Vectors bucket: {vector_bucket}")

        s3v.delete_vector_bucket(vectorBucketName=vector_bucket)
        logger.info(f"Deleted S3 Vectors bucket: {vector_bucket}")

    else:
        logger.warning(f"Unknown S3 Vectors resource type '{rtype}' — skipping {arn}")


def delete_cloudformation(session, arn: str):
    stack_name = arn.split("/")[1]
    cf = session.client("cloudformation")
    cf.delete_stack(StackName=stack_name)
    logger.info(f"Deleted CloudFormation stack: {stack_name}")


def delete_bedrock(session, arn: str):
    """
    Handles Bedrock taggable resources:
      - custom-model            arn:aws:bedrock:<r>:<a>:custom-model/<name>
      - model-import-job        arn:aws:bedrock:<r>:<a>:model-import-job/<id>
      - provisioned-model-throughput  arn:aws:bedrock:<r>:<a>:provisioned-model/<id>

    Note: Bedrock foundation models are AWS-managed and cannot be deleted.
    Provisioned throughput commitments with a term (1-month / 6-month) cannot
    be deleted before the term expires — the API will return a ValidationException
    which is caught and logged rather than raising.
    """
    bedrock = session.client("bedrock")
    # resource section is everything after the 5th colon
    resource_section = ":".join(arn.split(":")[5:])   # e.g. "custom-model/my-model"
    rtype = resource_section.split("/")[0]
    rid   = resource_section.split("/", 1)[-1]

    if rtype == "custom-model":
        bedrock.delete_custom_model(modelIdentifier=rid)
        logger.info(f"Deleted Bedrock custom model: {rid}")

    elif rtype == "model-import-job":
        # Import jobs cannot be deleted via API once completed; skip gracefully
        logger.info(f"Bedrock model-import-job '{rid}' — cannot be deleted via API, skipping")

    elif rtype == "provisioned-model":
        try:
            bedrock.delete_provisioned_model_throughput(provisionedModelId=rid)
            logger.info(f"Deleted Bedrock provisioned throughput: {rid}")
        except bedrock.exceptions.ValidationException as e:
            logger.warning(f"Cannot delete provisioned throughput '{rid}' (likely has a term commitment): {e}")

    else:
        logger.warning(f"Unknown Bedrock resource type '{rtype}' — skipping {arn}")


def delete_apprunner(session, arn: str):
    """
    Handles App Runner taggable resources:
      - service        arn:aws:apprunner:<r>:<a>:service/<name>/<id>
      - vpcconnector   arn:aws:apprunner:<r>:<a>:vpcconnector/<name>/<rev>/<id>

    Services must be deleted before VPC connectors they reference.
    Pausing first is optional but makes deletion faster for running services.
    """
    apprunner = session.client("apprunner")
    resource_section = ":".join(arn.split(":")[5:])
    rtype = resource_section.split("/")[0]

    if rtype == "service":
        apprunner.delete_service(ServiceArn=arn)
        logger.info(f"Deleted App Runner service: {arn}")

    elif rtype == "vpcconnector":
        apprunner.delete_vpc_connector(VpcConnectorArn=arn)
        logger.info(f"Deleted App Runner VPC connector: {arn}")

    else:
        logger.warning(f"Unknown App Runner resource type '{rtype}' — skipping {arn}")


def delete_sagemaker(session, arn: str):
    """
    Handles SageMaker taggable resources:
      - endpoint          (must delete before endpoint-config)
      - endpoint-config
      - model
      - notebook-instance (must be Stopped before deletion)
      - pipeline
      - domain            (must delete all user-profiles & apps first)
      - feature-group
      - training-job      (stop if InProgress, then it cannot be 'deleted'
                           — SageMaker retains job history; we stop it to end charges)
      - processing-job    (same as training-job)
      - compilation-job
      - hyper-parameter-tuning-job

    ARN format:  arn:aws:sagemaker:<region>:<account>:<resource-type>/<name>
    """
    sm    = session.client("sagemaker")
    parts = arn.split(":")
    # resource type and name sit in parts[5] as "<type>/<name>"
    resource_section = parts[5] if len(parts) > 5 else ""
    rtype = resource_section.split("/")[0]
    name  = resource_section.split("/", 1)[-1]

    if rtype == "endpoint":
        sm.delete_endpoint(EndpointName=name)
        logger.info(f"Deleted SageMaker endpoint: {name}")

    elif rtype == "endpoint-config":
        sm.delete_endpoint_config(EndpointConfigName=name)
        logger.info(f"Deleted SageMaker endpoint config: {name}")

    elif rtype == "model":
        sm.delete_model(ModelName=name)
        logger.info(f"Deleted SageMaker model: {name}")

    elif rtype == "notebook-instance":
        # Must be stopped before deletion
        try:
            sm.stop_notebook_instance(NotebookInstanceName=name)
            logger.info(f"Stopping SageMaker notebook instance: {name} (deletion will happen on next run or after stop completes)")
            # Note: deletion attempted immediately; AWS will reject if still stopping.
            # A more robust approach is to use a waiter, but that can block Lambda
            # for minutes. Instead we attempt and catch the error gracefully.
        except sm.exceptions.ClientError:
            pass
        try:
            sm.delete_notebook_instance(NotebookInstanceName=name)
            logger.info(f"Deleted SageMaker notebook instance: {name}")
        except sm.exceptions.ClientError as e:
            logger.warning(f"Could not delete notebook '{name}' (may still be stopping): {e}")

    elif rtype == "pipeline":
        sm.delete_pipeline(PipelineName=name)
        logger.info(f"Deleted SageMaker pipeline: {name}")

    elif rtype == "domain":
        # Domains require all user profiles and apps to be deleted first
        try:
            sm.delete_domain(DomainId=name, RetentionPolicy={"HomeEfsFileSystem": "Delete"})
            logger.info(f"Deleted SageMaker domain: {name}")
        except sm.exceptions.ResourceInUse as e:
            logger.warning(f"SageMaker domain '{name}' still has active apps/profiles — skipping: {e}")

    elif rtype == "feature-group":
        sm.delete_feature_group(FeatureGroupName=name)
        logger.info(f"Deleted SageMaker feature group: {name}")

    elif rtype == "training-job":
        # Training jobs cannot be deleted — only stopped to end compute charges
        try:
            sm.stop_training_job(TrainingJobName=name)
            logger.info(f"Stopped SageMaker training job: {name} (jobs cannot be fully deleted)")
        except sm.exceptions.ClientError as e:
            logger.info(f"Training job '{name}' already in terminal state: {e}")

    elif rtype == "processing-job":
        try:
            sm.stop_processing_job(ProcessingJobName=name)
            logger.info(f"Stopped SageMaker processing job: {name}")
        except sm.exceptions.ClientError as e:
            logger.info(f"Processing job '{name}' already in terminal state: {e}")

    elif rtype == "compilation-job":
        try:
            sm.stop_compilation_job(CompilationJobName=name)
            logger.info(f"Stopped SageMaker compilation job: {name}")
        except sm.exceptions.ClientError as e:
            logger.info(f"Compilation job '{name}' already in terminal state: {e}")

    elif rtype == "hyper-parameter-tuning-job":
        try:
            sm.stop_hyper_parameter_tuning_job(HyperParameterTuningJobName=name)
            logger.info(f"Stopped SageMaker HPT job: {name}")
        except ClientError as e:
            logger.info(f"HPT job '{name}' already in terminal state: {e}")

    # ── ML Lineage Tracking — auto-created alongside endpoint deployments ─────
    # These appear in tagged resource lists because they inherit tags from the
    # endpoint that created them. context, action, artifact, and association are
    # all part of SageMaker's lineage graph and must be cleaned up explicitly.

    elif rtype == "context":
        sm.delete_context(ContextName=name)
        logger.info(f"Deleted SageMaker lineage context: {name}")

    elif rtype == "action":
        sm.delete_action(ActionName=name)
        logger.info(f"Deleted SageMaker lineage action: {name}")

    elif rtype == "artifact":
        # Artifacts are identified by their full ARN, not just the name
        sm.delete_artifact(ArtifactArn=arn)
        logger.info(f"Deleted SageMaker lineage artifact: {name}")

    elif rtype == "association":
        # Associations link two lineage entities. The delete API requires
        # both SourceArn and DestinationArn, not the association ARN itself.
        # We list associations where this ARN is the destination to find the pair.
        try:
            resp = sm.list_associations(DestinationArn=arn)
            for assoc in resp.get("AssociationSummaries", []):
                sm.delete_association(
                    SourceArn=assoc["SourceArn"],
                    DestinationArn=assoc["DestinationArn"],
                )
                logger.info(f"Deleted SageMaker association: {assoc['SourceArn']} → {assoc['DestinationArn']}")
        except ClientError as e:
            logger.warning(f"Could not delete SageMaker association '{name}': {e}")

    else:
        logger.warning(f"Unknown SageMaker resource type '{rtype}' — skipping {arn}")


# ── Router ────────────────────────────────────────────────────────────────────

def delete_opensearch(session, arn: str):
    """
    Handles both OpenSearch Service flavours under the 'es' ARN prefix:

      OpenSearch/Elasticsearch managed domains:
        arn:aws:es:<r>:<a>:domain/<domain-name>

      OpenSearch Serverless collections:
        arn:aws:aoss:<r>:<a>:collection/<id>

    Note: 'es' is the service prefix for both classic managed domains AND
    the newer OpenSearch Serverless (aoss). The Tagging API returns 'es'
    for managed domains and 'aoss' for serverless — both are handled here
    via separate boto3 clients.

    Deletion caveats:
      - Managed domain deletion is asynchronous. The domain enters a
        'Deleting' state and takes 10–20 min to fully remove. The Lambda
        does not wait — it fires the delete and moves on.
      - Serverless collections also delete asynchronously.
      - OpenSearch Ingestion pipelines use the 'osis' service prefix and
        are handled as a separate entry in HANDLERS below.
    """
    resource_section = ":".join(arn.split(":")[5:])
    rtype = resource_section.split("/")[0]

    if rtype == "domain":
        # Classic managed OpenSearch / Elasticsearch domain
        domain_name = resource_section.split("/", 1)[1]
        os_client = session.client("opensearch")
        os_client.delete_domain(DomainName=domain_name)
        logger.info(f"Deleted OpenSearch managed domain: {domain_name}")

    elif rtype == "collection":
        # OpenSearch Serverless collection
        collection_id = resource_section.split("/", 1)[1]
        aoss = session.client("opensearchserverless")
        aoss.delete_collection(id=collection_id)
        logger.info(f"Deleted OpenSearch Serverless collection: {collection_id}")

    else:
        logger.warning(f"Unknown OpenSearch resource type '{rtype}' — skipping {arn}")


def delete_opensearch_ingestion(session, arn: str):
    """
    Handles OpenSearch Ingestion (OSIS) pipelines.

      arn:aws:osis:<r>:<a>:pipeline/<name>

    Pipelines must be in a non-transitioning state to be deleted.
    If a pipeline is 'Starting' or 'Stopping' the delete will fail —
    the handler catches this and logs a warning to retry next run.
    """
    osis = session.client("osis")
    pipeline_name = arn.split("/")[-1]
    try:
        osis.delete_pipeline(PipelineName=pipeline_name)
        logger.info(f"Deleted OpenSearch Ingestion pipeline: {pipeline_name}")
    except osis.exceptions.ConflictException as e:
        logger.warning(
            f"OpenSearch Ingestion pipeline '{pipeline_name}' is in a transitioning "
            f"state and cannot be deleted right now — will retry on next run: {e}"
        )


def _wait_efs_mount_targets_gone(efs_client, fs_id: str, max_wait: int = 120) -> None:
    """Mount targets must finish deleting before DeleteFileSystem succeeds."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        mts = efs_client.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", [])
        if not mts:
            return
        time.sleep(2)
    raise TimeoutError(f"EFS mount targets for {fs_id} still present after {max_wait}s")


def delete_eks(session, arn: str):
    """EKS clusters, node groups, and Fargate profiles (hourly control-plane + worker $)."""
    eks = session.client("eks")
    rtype = resource_type_of(arn)
    parts = arn.split("/")

    if rtype == "cluster":
        name = parts[-1]
        for ng in eks.list_nodegroups(clusterName=name).get("nodegroups", []):
            eks.delete_nodegroup(clusterName=name, nodegroupName=ng)
            logger.info(f"Deleting EKS node group: {ng}")
        for fp in eks.list_fargate_profiles(clusterName=name).get("fargateProfileNames", []):
            eks.delete_fargate_profile(clusterName=name, fargateProfileName=fp)
            logger.info(f"Deleting EKS Fargate profile: {fp}")
        eks.delete_cluster(name=name)
        logger.info(f"Deleted EKS cluster: {name}")

    elif rtype == "nodegroup":
        eks.delete_nodegroup(clusterName=parts[-2], nodegroupName=parts[-1])
        logger.info(f"Deleted EKS node group: {parts[-1]}")

    elif rtype == "fargateprofile":
        eks.delete_fargate_profile(clusterName=parts[-2], fargateProfileName=parts[-1])
        logger.info(f"Deleted EKS Fargate profile: {parts[-1]}")

    else:
        logger.warning(f"Unknown EKS resource type '{rtype}' — skipping {arn}")


def delete_elasticache(session, arn: str):
    """ElastiCache replication groups, cache clusters, and serverless caches."""
    ec = session.client("elasticache")
    parts = arn.split(":")
    if len(parts) < 7:
        logger.warning(f"Unexpected ElastiCache ARN — skipping {arn}")
        return
    rtype = parts[5]
    rid = parts[6]

    if rtype == "replicationgroup":
        ec.delete_replication_group(ReplicationGroupId=rid, RetainPrimaryCluster=False)
        logger.info(f"Deleted ElastiCache replication group: {rid}")

    elif rtype == "cluster":
        ec.delete_cache_cluster(CacheClusterId=rid)
        logger.info(f"Deleted ElastiCache cluster: {rid}")

    elif rtype == "serverlesscache":
        ec.delete_serverless_cache(ServerlessCacheName=rid)
        logger.info(f"Deleted ElastiCache Serverless cache: {rid}")

    else:
        logger.warning(f"Unknown ElastiCache resource type '{rtype}' — skipping {arn}")


def delete_efs(session, arn: str):
    """EFS file systems (storage + throughput $)."""
    efs = session.client("efs")
    fs_id = arn.split("/")[-1]
    for mt in efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", []):
        efs.delete_mount_target(MountTargetId=mt["MountTargetId"])
    if efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets"):
        _wait_efs_mount_targets_gone(efs, fs_id)
    efs.delete_file_system(FileSystemId=fs_id)
    logger.info(f"Deleted EFS file system: {fs_id}")


def delete_redshift(session, arn: str):
    """Redshift provisioned clusters."""
    rtype = resource_type_of(arn)
    if rtype != "cluster":
        logger.warning(f"Unknown Redshift resource type '{rtype}' — skipping {arn}")
        return
    name = arn.split("/")[-1]
    session.client("redshift").delete_cluster(
        ClusterIdentifier=name,
        SkipFinalClusterSnapshot=True,
    )
    logger.info(f"Deleted Redshift cluster: {name}")


def delete_redshift_serverless(session, arn: str):
    """Redshift Serverless namespaces and workgroups."""
    rs = session.client("redshift-serverless")
    sec = ":".join(arn.split(":")[5:])
    rtype = sec.split("/")[0]
    name = sec.split("/", 1)[-1]

    if rtype == "workgroup":
        rs.delete_workgroup(workgroupName=name)
        logger.info(f"Deleted Redshift Serverless workgroup: {name}")

    elif rtype == "namespace":
        rs.delete_namespace(namespaceName=name)
        logger.info(f"Deleted Redshift Serverless namespace: {name}")

    else:
        logger.warning(f"Unknown Redshift Serverless resource type '{rtype}' — skipping {arn}")


def delete_memorydb(session, arn: str):
    """MemoryDB clusters."""
    rtype = resource_type_of(arn)
    if rtype != "cluster":
        logger.warning(f"Unknown MemoryDB resource type '{rtype}' — skipping {arn}")
        return
    name = arn.split("/")[-1]
    session.client("memorydb").delete_cluster(ClusterName=name)
    logger.info(f"Deleted MemoryDB cluster: {name}")


def delete_kafka(session, arn: str):
    """Amazon MSK clusters."""
    session.client("kafka").delete_cluster(ClusterArn=arn)
    logger.info(f"Deleted MSK cluster: {arn}")


def delete_kinesis(session, arn: str):
    """Kinesis data streams."""
    rtype = resource_type_of(arn)
    if rtype != "stream":
        logger.warning(f"Unknown Kinesis resource type '{rtype}' — skipping {arn}")
        return
    name = arn.split("/")[-1]
    session.client("kinesis").delete_stream(StreamName=name, EnforceConsumerDeletion=True)
    logger.info(f"Deleted Kinesis stream: {name}")


def delete_firehose(session, arn: str):
    """Kinesis Data Firehose delivery streams."""
    rtype = resource_type_of(arn)
    if rtype != "deliverystream":
        logger.warning(f"Unknown Firehose resource type '{rtype}' — skipping {arn}")
        return
    name = arn.split("/")[-1]
    session.client("firehose").delete_delivery_stream(
        DeliveryStreamName=name,
        AllowForceDelete=True,
    )
    logger.info(f"Deleted Firehose delivery stream: {name}")


def delete_stepfunctions(session, arn: str):
    """Step Functions state machines."""
    session.client("stepfunctions").delete_state_machine(stateMachineArn=arn)
    logger.info(f"Deleted Step Functions state machine: {arn}")


def delete_events(session, arn: str):
    """EventBridge rules (custom and default event bus)."""
    events = session.client("events")
    sec = ":".join(arn.split(":")[5:])
    if not sec.startswith("rule/"):
        logger.warning(f"Unexpected EventBridge ARN — skipping {arn}")
        return
    rest = sec[5:]  # after "rule/"
    if "/" in rest:
        bus_name, rule_name = rest.split("/", 1)
    else:
        bus_name, rule_name = "default", rest
    tgt = events.list_targets_by_rule(Rule=rule_name, EventBusName=bus_name)
    ids = [t["Id"] for t in tgt.get("Targets", [])]
    if ids:
        events.remove_targets(Rule=rule_name, EventBusName=bus_name, Ids=ids, Force=True)
    events.delete_rule(Name=rule_name, EventBusName=bus_name)
    logger.info(f"Deleted EventBridge rule: {rule_name} on {bus_name}")


def delete_ecr(session, arn: str):
    """ECR repositories (storage $)."""
    if not arn.startswith("arn:aws:ecr:"):
        logger.warning(f"Unexpected ECR ARN — skipping {arn}")
        return
    # arn:aws:ecr:region:account:repository/name/with/slashes  (no extra colons)
    tail = arn.split(":", 5)[-1]
    if not tail.startswith("repository/"):
        logger.warning(f"Unknown ECR resource — skipping {arn}")
        return
    repo = tail[len("repository/") :]
    session.client("ecr").delete_repository(repositoryName=repo, force=True)
    logger.info(f"Deleted ECR repository: {repo}")


def delete_secretsmanager(session, arn: str):
    """Secrets Manager secrets (per-secret monthly $)."""
    session.client("secretsmanager").delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
    logger.info(f"Deleted secret: {arn}")


def delete_glue(session, arn: str):
    """Glue crawlers and jobs (glue DPU / crawler $)."""
    glue = session.client("glue")
    sec = ":".join(arn.split(":")[5:])
    if "/" not in sec:
        logger.warning(f"Unexpected Glue ARN — skipping {arn}")
        return
    rtype, name = sec.split("/", 1)

    if rtype == "crawler":
        glue.delete_crawler(Name=name)
        logger.info(f"Deleted Glue crawler: {name}")

    elif rtype == "job":
        glue.delete_job(JobName=name)
        logger.info(f"Deleted Glue job: {name}")

    else:
        logger.warning(f"Unknown Glue resource type '{rtype}' — skipping {arn}")


def delete_emr(session, arn: str):
    """EMR clusters (EC2-backed hourly $)."""
    rtype = resource_type_of(arn)
    if rtype != "cluster":
        logger.warning(f"Unknown EMR resource type '{rtype}' — skipping {arn}")
        return
    job_flow_id = arn.split("/")[-1]
    session.client("emr").terminate_job_flows(JobFlowIds=[job_flow_id])
    logger.info(f"Terminated EMR cluster: {job_flow_id}")


def delete_amplify(session, arn: str):
    """Amplify apps (hosting build + serving $)."""
    app_id = arn.split("/")[-1]
    session.client("amplify").delete_app(appId=app_id)
    logger.info(f"Deleted Amplify app: {app_id}")


def delete_mq(session, arn: str):
    """Amazon MQ brokers."""
    parts = arn.split(":")
    if len(parts) < 7 or parts[5] != "broker":
        logger.warning(f"Unexpected MQ ARN — skipping {arn}")
        return
    broker_id = parts[6]
    session.client("mq").delete_broker(BrokerId=broker_id)
    logger.info(f"Deleted MQ broker: {broker_id}")


def delete_dms(session, arn: str):
    """DMS replication instances (instance hourly $)."""
    parts = arn.split(":")
    if len(parts) < 7 or parts[5] != "rep":
        logger.warning(f"Unexpected DMS ARN (only replication instances supported) — skipping {arn}")
        return
    session.client("dms").delete_replication_instance(ReplicationInstanceArn=arn)
    logger.info(f"Deleted DMS replication instance: {arn}")


def delete_batch(session, arn: str):
    """Batch compute environments."""
    rtype = resource_type_of(arn)
    if rtype != "compute-environment":
        logger.warning(f"Unknown Batch resource type '{rtype}' — skipping {arn}")
        return
    name = arn.split("/")[-1]
    b = session.client("batch")
    b.update_compute_environment(computeEnvironment=name, state="DISABLED")
    b.delete_compute_environment(computeEnvironment=name)
    logger.info(f"Deleted Batch compute environment: {name}")


def delete_apigateway_rest(session, arn: str):
    """API Gateway REST APIs (v1)."""
    rest_api_id = arn.rstrip("/").split("/")[-1]
    session.client("apigateway").delete_rest_api(restApiId=rest_api_id)
    logger.info(f"Deleted API Gateway REST API: {rest_api_id}")


def delete_apigatewayv2(session, arn: str):
    """API Gateway HTTP and WebSocket APIs (v2)."""
    sec = arn.split(":")[5]
    if not sec.startswith("api/"):
        logger.warning(f"Unexpected API Gateway v2 ARN — skipping {arn}")
        return
    api_id = sec.split("/", 1)[1]
    session.client("apigatewayv2").delete_api(ApiId=api_id)
    logger.info(f"Deleted API Gateway v2 API: {api_id}")


def delete_execute_api(session, arn: str):
    """execute-api ARNs for HTTP APIs — delete via API Gateway v2."""
    part = arn.split(":")[5]
    api_id = part.split("/")[0]
    session.client("apigatewayv2").delete_api(ApiId=api_id)
    logger.info(f"Deleted API (execute-api ARN): {api_id}")


def delete_appsync(session, arn: str):
    """AppSync GraphQL APIs."""
    sec = arn.split(":")[5]
    if not sec.startswith("apis/"):
        logger.warning(f"Unexpected AppSync ARN — skipping {arn}")
        return
    api_id = sec.split("/", 1)[1]
    session.client("appsync").delete_graphql_api(apiId=api_id)
    logger.info(f"Deleted AppSync API: {api_id}")


def delete_logs(session, arn: str):
    """CloudWatch Logs log groups (ingestion + storage $)."""
    parts = arn.split(":")
    if len(parts) < 7 or parts[5] != "log-group":
        logger.warning(f"Unexpected Logs ARN — skipping {arn}")
        return
    name = ":".join(parts[6:]).removesuffix(":*")
    session.client("logs").delete_log_group(logGroupName=name)
    logger.info(f"Deleted log group: {name}")


def delete_docdb_elastic(session, arn: str):
    """Amazon DocumentDB elastic clusters."""
    session.client("docdb-elastic").delete_cluster(clusterArn=arn)
    logger.info(f"Deleted DocumentDB Elastic cluster: {arn}")


def delete_timestream(session, arn: str):
    """
    Amazon Timestream — single table or whole database.

    Table ARN:  arn:aws:timestream:region:account:database/dbname/table/tablename
    Database:   arn:aws:timestream:region:account:database/dbname
    """
    tw = session.client("timestream-write")
    sec = ":".join(arn.split(":")[5:])
    if not sec.startswith("database/"):
        logger.warning(f"Unknown Timestream resource — skipping {arn}")
        return

    if "/table/" in sec:
        head, _, table_name = sec.partition("/table/")
        db_name = head[len("database/") :]
        if not db_name or not table_name:
            logger.warning(f"Malformed Timestream table ARN — skipping {arn}")
            return
        tw.delete_table(DatabaseName=db_name, TableName=table_name)
        logger.info(f"Deleted Timestream table: {table_name} (database {db_name})")
        return

    db_name = sec[len("database/") :]
    paginator = tw.get_paginator("list_tables")
    for page in paginator.paginate(DatabaseName=db_name):
        for t in page.get("Tables", []):
            tw.delete_table(DatabaseName=db_name, TableName=t["TableName"])
    tw.delete_database(DatabaseName=db_name)
    logger.info(f"Deleted Timestream database: {db_name}")


def delete_globalaccelerator(session, arn: str):
    """
    Global Accelerator (fixed fee + data processing $).
    API is only available in us-west-2, us-east-1, us-east-2, eu-west-1, etc.
    """
    # ARN has no region segment; boto3 needs a regional endpoint for this control plane.
    ga = boto3.Session(region_name="us-west-2").client("globalaccelerator")
    ga.delete_accelerator(AcceleratorArn=arn)
    logger.info(f"Deleted Global Accelerator: {arn}")


def sort_arns_for_deletion(arns: list[str]) -> list[str]:
    """
    Order deletes so dependents go before parents (RDS proxies before instances they
    target, instances before clusters, EKS nodegroups/Fargate before cluster,
    ECS services before cluster, EC2 NAT gateways before their Elastic IPs, etc.).
    """
    def key(a: str) -> tuple:
        svc = service_of(a)
        r = resource_type_of(a)
        if svc == "rds":
            # Proxy must go before db — proxy targets instances; delete proxy first.
            order = {
                "db-proxy": 0,
                "db": 1,
                "snapshot": 2,
                "cluster-snapshot": 3,
                "cluster": 4,
            }
            return (0, order.get(r, 99), a)
        if svc == "eks":
            order = {"nodegroup": 0, "fargateprofile": 1, "cluster": 2}
            return (1, order.get(r, 99), a)
        if svc == "ecs":
            order = {"service": 0, "cluster": 1}
            return (2, order.get(r, 99), a)
        if svc == "redshift-serverless":
            sec = ":".join(a.split(":")[5:])
            rt = sec.split("/")[0]
            order = {"workgroup": 0, "namespace": 1}
            return (3, order.get(rt, 99), a)
        if svc == "ec2":
            # NAT gateways hold an allocation — release NAT before the EIP.
            order = {
                "natgateway": 0,
                "internet-gateway": 1,
                "elastic-ip": 2,
                "vpc-endpoint": 3,
            }
            return (4, order.get(r, 50), a)
        return (9, 0, a)

    return sorted(arns, key=key)


HANDLERS = {
    "ec2":                   delete_ec2,
    "rds":                   delete_rds,
    "ecs":                   delete_ecs,
    "elasticloadbalancing":  delete_elasticloadbalancing,
    "lambda":                delete_lambda_fn,
    "sqs":                   delete_sqs,
    "sns":                   delete_sns,
    "dynamodb":              delete_dynamodb,
    "s3":                    empty_and_delete_s3,
    "s3vectors":             delete_s3vectors,
    "cloudformation":        delete_cloudformation,
    "bedrock":               delete_bedrock,
    "apprunner":             delete_apprunner,
    "sagemaker":             delete_sagemaker,
    "es":                    delete_opensearch,
    "aoss":                  delete_opensearch,
    "osis":                  delete_opensearch_ingestion,
    "eks":                   delete_eks,
    "elasticache":           delete_elasticache,
    "elasticfilesystem":     delete_efs,
    "redshift":              delete_redshift,
    "redshift-serverless":   delete_redshift_serverless,
    "memorydb":              delete_memorydb,
    "kafka":                 delete_kafka,
    "kinesis":               delete_kinesis,
    "firehose":              delete_firehose,
    "states":                delete_stepfunctions,
    "events":                delete_events,
    "ecr":                   delete_ecr,
    "secretsmanager":        delete_secretsmanager,
    "glue":                  delete_glue,
    "elasticmapreduce":      delete_emr,
    "amplify":               delete_amplify,
    "mq":                    delete_mq,
    "dms":                   delete_dms,
    "batch":                 delete_batch,
    "apigateway":            delete_apigateway_rest,
    "apigatewayv2":          delete_apigatewayv2,
    "execute-api":           delete_execute_api,
    "appsync":               delete_appsync,
    "logs":                  delete_logs,
    "docdb-elastic":         delete_docdb_elastic,
    "timestream":            delete_timestream,
    "globalaccelerator":     delete_globalaccelerator,
}


def dispatch(session, arn: str):
    r = region_of(arn)
    regional = boto3.Session(region_name=r) if r else session
    service = service_of(arn)
    handler = HANDLERS.get(service)
    if handler:
        try:
            handler(regional, arn)
        except ClientError as e:
            logger.error(f"Failed to delete {arn}: {e.response['Error']['Message']}")
    else:
        logger.warning(f"No handler for service '{service}' — skipping {arn}")


# ── Entry point ───────────────────────────────────────────────────────────────

def handler(event, context):
    session = boto3.Session()
    logger.info("Starting nightly cleanup — finding resources tagged auto-delete=true")

    arns = sort_arns_for_deletion(get_all_tagged_resources(session))
    logger.info(f"Found {len(arns)} resource(s) to delete")

    deleted, failed = 0, 0
    for arn in arns:
        try:
            dispatch(session, arn)
            deleted += 1
        except Exception as e:
            logger.error(f"Unexpected error for {arn}: {e}")
            failed += 1

    summary = {
        "total_found":   len(arns),
        "deleted":       deleted,
        "failed":        failed,
        "resources":     arns,
    }
    logger.info(f"Cleanup complete: {deleted} deleted, {failed} failed")
    return summary

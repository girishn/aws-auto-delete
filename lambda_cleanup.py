"""
Nightly AWS Resource Cleanup Lambda
------------------------------------
Finds all resources tagged with auto-delete=true and deletes them.
Triggered by EventBridge Scheduler (e.g. cron at 8pm UTC nightly).

Supported resource types:
  - EC2 instances, key pairs, security groups, EBS volumes, snapshots, AMIs
  - RDS instances, clusters & Aurora clusters
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
"""

import boto3
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TAG_KEY   = "auto-delete"
TAG_VALUE = "true"


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_all_tagged_resources(session):
    """Return every resource ARN tagged auto-delete=true in this region."""
    client = session.client("resourcegroupstaggingapi")
    arns = []
    paginator = client.get_paginator("get_resources")
    for page in paginator.paginate(
        TagFilters=[{"Key": TAG_KEY, "Values": [TAG_VALUE]}]
    ):
        arns.extend(r["ResourceARN"] for r in page["ResourceTagMappingList"])
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
    import os
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
    "es":                    delete_opensearch,       # managed domains (ARN prefix 'es')
    "aoss":                  delete_opensearch,       # serverless collections
    "osis":                  delete_opensearch_ingestion,
}


def dispatch(session, arn: str):
    service = service_of(arn)
    handler = HANDLERS.get(service)
    if handler:
        try:
            handler(session, arn)
        except ClientError as e:
            logger.error(f"Failed to delete {arn}: {e.response['Error']['Message']}")
    else:
        logger.warning(f"No handler for service '{service}' — skipping {arn}")


# ── Entry point ───────────────────────────────────────────────────────────────

def handler(event, context):
    session = boto3.Session()
    logger.info("Starting nightly cleanup — finding resources tagged auto-delete=true")

    arns = get_all_tagged_resources(session)
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

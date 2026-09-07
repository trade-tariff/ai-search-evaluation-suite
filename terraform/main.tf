module "service" {
  # TODO: bump to a real released tag once trade-tariff-platform-terraform-modules#108 merges and gets one.
  source = "git@github.com:trade-tariff/trade-tariff-platform-terraform-modules.git//aws/ecs-service?ref=7f0a13b73244e19e750c6b4719cdcd8673c58ff0"

  region = var.region

  service_name  = "eval"
  service_count = var.service_count

  cluster_name    = "trade-tariff-cluster-${var.environment}"
  subnet_ids      = data.aws_subnets.private.ids
  security_groups = [data.aws_security_group.this.id]

  container_port = 8443

  # Reuses the Dockerfile's own HEALTHCHECK command/timings - ECS otherwise has
  # no application-level health signal for an internal-only service with no ALB.
  container_health_check = {
    command = [
      "CMD", "python", "-c",
      "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/api/health', context=ssl._create_unverified_context()).read()"
    ]
  }

  cloudwatch_log_group_name = "platform-logs-${var.environment}"

  docker_image = "382373577178.dkr.ecr.eu-west-2.amazonaws.com/tariff-ai-search-evaluation-suite-production"
  docker_tag   = var.docker_tag
  skip_destroy = true

  private_dns_namespace = "tariff.internal"

  cpu    = var.cpu
  memory = var.memory

  task_role_policy_arns      = [aws_iam_policy.task.arn]
  execution_role_policy_arns = [aws_iam_policy.execution.arn]
  enable_ecs_exec            = true

  service_environment_config = local.service_environment
  service_secrets_config     = local.eval_secrets_config

  has_autoscaler = false
  min_capacity   = 1
  max_capacity   = 1

  enable_alarms       = var.enable_alarms
  cpu_alarm_threshold = 75

  sns_topic_arns               = [data.aws_sns_topic.slack.arn]
  observability_sns_topic_arns = var.enable_observability_alerts ? [data.aws_sns_topic.observability[0].arn] : null
}

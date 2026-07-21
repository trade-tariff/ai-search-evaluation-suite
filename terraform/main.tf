module "service" {
  source = "git@github.com:trade-tariff/trade-tariff-platform-terraform-modules.git//aws/ecs-service?ref=aws/ecs-service-v3.1.0"

  region = var.region

  service_name  = "eval"
  service_count = var.service_count

  cluster_name    = "trade-tariff-cluster-${var.environment}"
  subnet_ids      = data.aws_subnets.private.ids
  security_groups = [data.aws_security_group.this.id]

  target_group_arn = data.aws_lb_target_group.this.arn
  container_port   = 8443

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

  has_autoscaler = false
  min_capacity   = 1
  max_capacity   = 1

  enable_alarms       = var.enable_alarms
  cpu_alarm_threshold = 75

  sns_topic_arns               = [data.aws_sns_topic.slack.arn]
  observability_sns_topic_arns = var.enable_observability_alerts ? [data.aws_sns_topic.observability[0].arn] : null
}

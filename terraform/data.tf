data "aws_vpc" "this" {
  tags = { Name = "trade-tariff-${var.environment}-vpc" }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.this.id]
  }

  tags = { Name = "*private*" }
}

data "aws_security_group" "this" {
  name = "trade-tariff-ecs-security-group-${var.environment}"
}

data "aws_kms_key" "secrets" {
  key_id = "alias/secretsmanager-key"
}

data "aws_secretsmanager_secret" "ecs_tls_certificate" {
  name = "ecs-tls-certificate"
}

data "aws_secretsmanager_secret_version" "ecs_tls_certificate" {
  secret_id = data.aws_secretsmanager_secret.ecs_tls_certificate.id
}

data "aws_secretsmanager_secret" "eval_api_configuration" {
  name = "eval-api-configuration"
}

data "aws_secretsmanager_secret_version" "eval_api_configuration" {
  secret_id = data.aws_secretsmanager_secret.eval_api_configuration.id
}

data "aws_sns_topic" "slack" {
  name = "slack-topic"
}

data "aws_sns_topic" "observability" {
  count = var.enable_observability_alerts ? 1 : 0
  name  = "slack-observability-topic"
}

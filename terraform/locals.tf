locals {
  tls_secret = jsondecode(data.aws_secretsmanager_secret_version.ecs_tls_certificate.secret_string)

  tls_env_vars = [
    {
      name  = "SSL_KEY_PEM"
      value = local.tls_secret.private_key
    },
    {
      name  = "SSL_CERT_PEM"
      value = local.tls_secret.certificate
    },
    {
      name  = "SSL_PORT"
      value = "8443"
    },
  ]

  eval_secrets_config = [
    {
      name      = "OPENAI_API_KEY"
      valueFrom = "${data.aws_secretsmanager_secret.eval_api_configuration.arn}:OPENAI_API_KEY::"
    },
    {
      name      = "AI_FAN_OUT_BASIC_AUTH_USER"
      valueFrom = "${data.aws_secretsmanager_secret.eval_api_configuration.arn}:AI_FAN_OUT_BASIC_AUTH_USER::"
    },
    {
      name      = "AI_FAN_OUT_BASIC_AUTH_PASSWORD"
      valueFrom = "${data.aws_secretsmanager_secret.eval_api_configuration.arn}:AI_FAN_OUT_BASIC_AUTH_PASSWORD::"
    },
    {
      name      = "CLASSIFICATION_ALLOW_PROVIDER_CALLS"
      valueFrom = "${data.aws_secretsmanager_secret.eval_api_configuration.arn}:CLASSIFICATION_ALLOW_PROVIDER_CALLS::"
    },
  ]

  backend_url_env_vars = [
    {
      name  = "TRADE_TARIFF_BACKEND_BASE_URL"
      value = "https://backend-uk.tariff.internal:8443"
    },
  ]

  auth_env_vars = [
    {
      name  = "AI_FAN_OUT_AUTH_PUBLIC_PATHS"
      value = "/api/health"
    },
  ]

  service_environment = concat(local.tls_env_vars, local.backend_url_env_vars, local.auth_env_vars)
}

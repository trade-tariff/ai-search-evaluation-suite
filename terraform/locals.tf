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
  ]

  backend_url_env_vars = [
    {
      name  = "TRADE_TARIFF_BACKEND_BASE_URL"
      value = "https://backend-uk.tariff.internal:8443"
    },
  ]

  service_environment = concat(local.tls_env_vars, local.backend_url_env_vars)
}

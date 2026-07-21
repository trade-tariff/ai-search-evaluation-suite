locals {
  tls_secret = jsondecode(data.aws_secretsmanager_secret_version.ecs_tls_certificate.secret_string)

  service_environment = [
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
}

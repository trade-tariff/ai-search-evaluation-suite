# AI Search Evaluation Suite Terraform

Deploys the backend-only `ai-eval` placeholder service to the shared Trade Tariff ECS platform. The service has no database or application configuration: it receives only the shared in-container TLS certificate and serves `OKAY` from `/` and `/healthcheckz`.

The shared ECS deployment workflow currently reads `.ruby-version` for release metadata even for non-Ruby images. The repository-level file exists only for compatibility with that workflow; Ruby is not included in the image.

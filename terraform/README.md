# AI Search Evaluation Suite Terraform

Deploys the `ai-search-evaluation-suite` classification-evals app to the shared Trade Tariff ECS platform. The service registers as `eval.tariff.internal` for private service-to-service access only (no public route). It reads its OpenAI API key from the `eval-api-configuration` Secrets Manager secret and the shared in-container TLS certificate, same as every other service in this account.

The shared ECS deployment workflow currently reads `.ruby-version` for release metadata even for non-Ruby images. The repository-level file exists only for compatibility with that workflow; Ruby is not included in the image.

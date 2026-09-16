# Agent Instructions

## Start Here

Read these first:

- `README.md`
- `docs/PATH_TO_PROD.md`
- `docs/DEMO_PLAYBOOK.md`
- `docs/MEASUREMENT_REVIEW.md`
- `docs/suite-primer-run.md`

AI Search Evaluation Suite is a standalone deployable app for tariff classification and search evaluation. It combines a React workbench, FastAPI services, retrieval and classification evaluation runners, knowledge graph tooling, ATaR workflows, intercept analysis, and provider benchmarking.

## Working Rules

- Keep scope tight and prefer simple changes.
- Verify generated or AI-suggested claims against source code and tests.
- Use `rg` for code search.
- Follow existing patterns in the repository.

## Pull request writing

Write all pull request titles and descriptions in ASD-STE100 Simplified Technical English.

- Write short sentences. Put one fact or one instruction in each sentence.
- Use the same word for the same thing. Do not use synonyms.
- Use active voice.
- Use simple present tense for facts. Use simple past tense for completed work.
- Use words that a general technical reader can understand. Keep product names and legal terms when they are necessary.
- Do not write long noun clusters.
- Do not write filler, marketing language, or vague claims.
- Follow the repository pull request template. Fill each required section with concrete facts.
- Write for the reviewer. Do not write a work diary.

## Pull requests and commits

- Follow `.github/pull_request_template.md`.
- Use conventional commits.
- Apply exactly one risk label.

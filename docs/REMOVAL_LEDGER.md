# Removal ledger

This ledger records the approved removals, their prerequisites, and retention
decisions. Each removal and prerequisite has a separate pull request. The links
below record the reviews and merge commits.

## Status

The approved queue is closed. F01-F13 and W01-W08 are merged. P01-P04 are merged.
R01 retains the independent Journey service and its deployment definitions.
R02 retains research source and the existing extraction profiles. No further
approved deletions or pending disposition decisions remain.

## Contract to preserve

Admin creates and tracks runs through trade-tariff-backend. The backend starts
the eval worker with `POST /api/evaluation/runs/{run_id}/start`. The worker reads
run configuration and gold queries from the backend, submits and polls searches,
posts results, and updates run status. Keep authentication, provider-spend gates,
health checks, cancellation, result scoring, and structured logging.

Do not delete data, run paid evaluations, or change the Admin/backend API
contract as part of these removals. Source reachability does not establish
operator use. Recheck callers and open pull requests before each removal.

## Frontend removals

Paths in this table are relative to `apps/product/frontend/src/`. The queue
removed callers before their dependencies so that TypeScript could check the
remaining source. Each merged entry records a pull request and merge commit.

| ID | Candidate | State | Pull request / merge commit |
| --- | --- | --- | --- |
| F01 | `components/CompareTab.tsx` | Merged | [#51](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/51) / `77ef136d2986c4fe6464c14863d8fddd9ffced49` |
| F02 | `components/TraderJourneyTab.tsx` | Merged | [#52](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/52) / `186a3336cff0d3d0a078334c7e33c1af078d3aff` |
| F03 | `trader-journey/stages/CompareStage.tsx` | Merged | [#53](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/53) / `cdb008607430333539a175d1043505b1652e5f0d` |
| P01 | Move `Stage` to `trader-journey/types.ts` and update App/Stepper imports | Merged | [#54](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/54) / `15212f1c367d3badfaf60b5fd2e5197c6011f801` |
| F04 | `trader-journey/TraderJourneyApp.tsx` | Merged | [#55](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/55) / `a57fff7764121e48f2b91b33f833405f45602a78` |
| F05 | `trader-journey/stages/ClassifyStage.tsx` | Merged | [#56](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/56) / `8bd3b2f05e0170fecffb7277accaf601f5ee34ea` |
| F06 | `trader-journey/stages/ValueStage.tsx` | Merged | [#58](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/58) / `a03941d1b7a9aed8757b7728a24bfa0c4269eb1e` |
| F07 | `trader-journey/stages/DutyStage.tsx` | Merged | [#59](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/59) / `52b4ee521f09c3666527094c2d26c14d0be8e891` |
| F08 | `trader-journey/stages/LandedStage.tsx` | Merged | [#60](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/60) / `12e5e1c68570472ed0aacca3fec34258c23ce732` |
| F09 | `trader-journey/stages/DeclareStage.tsx` | Merged | [#61](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/61) / `d072987e2a5b570b7a891616e47da1e4692d9a35` |
| F10 | `trader-journey/components/Stepper.tsx` | Merged | [#62](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/62) / `0f0db62e50f851bbad32c2ca756e0c3f95685f91` |
| F11 | `trader-journey/components/Money.tsx` | Merged | [#63](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/63) / `21b138cbe15159385445c18d86a81e562871631f` |
| F12 | `trader-journey/api.ts` | Merged | [#64](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/64) / `bbf63bc47217ed7290b06fd79a226105aca251b3` |
| F13 | `trader-journey/types.ts` | Merged | [#65](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/65) / `d7cd5f484332481c78046f2f569f82e2aac6b461` |

P01 moved `Stage` to the shared types file before F04 so that Stepper could
still type-check. F05-F12 removed the remaining Journey components and API
client. F13 removed the shared types file after those consumers.

The workbench entrypoint `src/main.tsx` renders only `App`. It does not import
the Journey code. [PR #37](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/37)
proposed a separate journey entrypoint but is closed without merge. The frontend
removals supersede that proposal. They do not remove the separate Journey
backend, its API, or its deployment files. R01 retains that independent service
and its deployment definitions for the reasons below.

## Worker and research separation

These entries record the worker and research separation after the frontend
removals. Prerequisites stayed separate from deletions so that each change could
be reviewed independently.

| ID | Candidate or prerequisite | State | Pull request / merge commit |
| --- | --- | --- | --- |
| W01 | Direct offline tests for the simulator used by the backend-driven worker | Merged | [#66](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/66) / `58482da70161a3b072e961afa91513556511b004` |
| W02 | Separate shared simulator/session facts from local classification and DB imports | Merged | [#67](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/67) / `a1375d525679a50b213fcc5d5c2769708477fc6d` |
| W03 | Separate the run-start ingress from the product workbench module | Merged | [#68](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/68) / `6cb8620f1c3d058a3791ce60d2b530507f70b784` |
| W04 | Remove copied research APIs from the worker app | Merged | [#69](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/69) / `b0e7932b4c10bdf43f72e95f9f65cea5edb3918f` |
| W05 | Remove standalone research job, trial, and matrix routes from the worker app | Merged | [#70](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/70) / `baf3fca816e86a8606dbc7deb8b25ecd90a1e6be` |
| P02 | Include extracted worker modules in the secondary image | Merged | [#71](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/71) / `e5b334f5c21dd4d957cac89209e3c8a28f2c9ffb` |
| P03 | Isolate worker runtime dependencies in both images | Merged | [#73](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/73) / `1205aa5988de219068e809fceeb6a30261631ff4` |
| W06 | Remove the React bundle and workbench routes from the worker image | Merged | [#72](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/72) / `e44d633b3bb4b96502acbf1382115051067e771b` |
| P04 | Preserve the existing extraction Compose profiles with a separate image target | Merged | [#74](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/74) / `3c6fdca18f5a271779468b2b207930a8ec08815b` |
| W07 | Remove research-only code and dependencies from the worker image | Merged | [#75](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/75) / `1aa7c952fe5ac38b7c5021b02586c32c0e7467a1` |
| W08 | Remove obsolete placeholder/in-use claims and document the worker contract | Merged | [#77](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/77) / `296c782fd011d6f6ab74c95b0af2c0e12e37da43` |
| R01 | Decide whether to retain or archive the separate journey backend and deployment | Retain source and existing deployment definitions | [#76](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/76) / `637958043cdef3e13407bff7c35defee661db15f` |
| R02 | Decide whether to retain or archive extraction, matrix, MCP, and experiment tools | Retain source and research entrypoint; excluded from worker images | [#69](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/69) / `b0e7932b4c10bdf43f72e95f9f65cea5edb3918f` |

P03 separates the worker requirements from the full research requirements.
The worker uses FastAPI, Uvicorn, HTTPX, and OpenAI. This excludes the unused
Anthropic SDK that caused the W06 image scan to fail. It does not add scanner
exceptions.

P04 keeps the existing manual and scheduler extraction profiles on the
`extraction-tools` image target. That target includes the retained scripts,
seed data, and full research requirements. The default target remains the worker.
This preserves the existing profiles; it adds no service or deployment.

W07 limits both worker images to the ingress, backend client and runner, shared
simulator, authentication, logging, and worker app. The worker CLI stays in its
backend package. Research APIs and tools stay in the repository. The extraction
image keeps the source and seed data for its existing Compose profiles. Neither
worker image includes that research source or seed data. Empty directories remain
for the ownership of existing Compose volumes. The local worker launcher installs
only the worker requirements.

R01 retains `apps/product/backend/journey/`, `apps/product/Dockerfile.journey`,
and the Journey service in `apps/product/docker-compose.yml`. The Dockerfile
launches `journey.main:app`; Compose selects that Dockerfile and exposes its
health endpoint on port 8200. These entrypoints are independent of the removed
frontend files and the Admin worker. Source wiring proves reachability, not
operator use. No operator-use evidence or service-retirement decision supports
deleting this service. Its source and deployment definitions therefore stay.
This decision adds no deployment and does not establish that the Journey UI works.

Research code that is not part of the Admin flow is not automatically dead.
The journey has its own Dockerfile and Compose service. Extraction scripts have
explicit CLI and scheduled entrypoints. Preserve historical experiment evidence.
R02 retains those reachable tools and the workbench API source. The optional
`backend.research_app:app` entrypoint preserves the copied research APIs without
loading them in the worker. This adds no research deployment. Default launchers
select `backend.worker_app:app`. The native research implementation stays in
`backend.app`; W07 excludes it from the worker image. Operator use remains
unverified; removal needs a separate retirement decision.

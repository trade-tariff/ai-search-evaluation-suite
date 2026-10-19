# Removal ledger

Remove one candidate per pull request. Obtain a Copilot review, address its
findings, and let the repository auto-merger merge the pull request before
starting the next candidate. Record the merged pull request and commit here.

## Contract to preserve

Admin creates and tracks runs through trade-tariff-backend. The backend starts
the eval worker with `POST /api/evaluation/runs/{run_id}/start`. The worker reads
run configuration and gold queries from the backend, submits and polls searches,
posts results, and updates run status. Keep authentication, provider-spend gates,
health checks, cancellation, result scoring, and structured logging.

Do not delete data, run paid evaluations, or change the Admin/backend API
contract as part of these removals. Source reachability does not establish
operator use. Recheck callers and open pull requests before each removal.

## Frontend deletion queue

Paths in this table are relative to `apps/product/frontend/src/`. Remove callers
before their dependencies so that TypeScript can still check the remaining
source files. A merged entry needs a pull request link and merge commit.

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
| F13 | `trader-journey/types.ts` | In progress | Pending |

P01 moved `Stage` to the shared types file before F04 so that Stepper could
still type-check. F05-F12 removed the remaining Journey components and API
client. F13 removes the shared types file after those consumers.

The workbench entrypoint `src/main.tsx` renders only `App`. It does not import
the Journey code. [PR #37](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/37)
proposed a separate journey entrypoint but is closed without merge. The frontend
removals supersede that proposal. They do not remove the separate Journey
backend, its API, or its deployment files. R01 still requires a decision on that
independent service.

## Worker and research separation

These candidates remain in scope after the independent frontend removals.
Take each row through its own pull request. Keep prerequisites separate from
deletions when they can be reviewed independently.

| ID | Candidate or prerequisite | State | Pull request / merge commit |
| --- | --- | --- | --- |
| W01 | Direct offline tests for the simulator used by the backend-driven worker | Queued prerequisite | |
| W02 | Separate shared simulator/session facts from local classification and DB imports | Depends on W01 | |
| W03 | Separate the run-start ingress from the product workbench module | Depends on W02 | |
| W04 | Remove copied research APIs from the worker app | Depends on W03 | |
| W05 | Remove standalone research job, trial, and matrix routes from the worker app | Depends on W04 | |
| W06 | Remove the React bundle and workbench routes from the worker image | Depends on W05 | |
| W07 | Remove research-only code and dependencies from the worker image | Depends on W06 | |
| W08 | Remove obsolete placeholder/in-use claims and document the worker contract | Depends on worker removals | |
| R01 | Decide whether to retain or archive the separate journey backend and deployment | Ownership review pending; frontend proposal #37 closed | |
| R02 | Decide whether to retain or archive extraction, matrix, MCP, and experiment tools | Ownership review pending | |

Research code that is not part of the Admin flow is not automatically dead.
The journey has its own Dockerfile and Compose service. Extraction scripts have
explicit CLI and scheduled entrypoints. Preserve historical experiment evidence.
Record a source-backed retention or archive decision for R01 and R02 before
closing this ledger. Do not report the campaign complete while any deletion or
required decision remains unresolved.

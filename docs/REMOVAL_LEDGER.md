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
| F03 | `trader-journey/stages/CompareStage.tsx` | In progress | Pending |
| P01 | Move `Stage` to `trader-journey/types.ts` and update App/Stepper imports | Prerequisite before F04 | |
| F04 | `trader-journey/TraderJourneyApp.tsx` | Depends on P01 | |
| F05 | `trader-journey/stages/ClassifyStage.tsx` | Depends on F04 | |
| F06 | `trader-journey/stages/ValueStage.tsx` | Depends on F04 | |
| F07 | `trader-journey/stages/DutyStage.tsx` | Depends on F04 | |
| F08 | `trader-journey/stages/LandedStage.tsx` | Depends on F04 | |
| F09 | `trader-journey/stages/DeclareStage.tsx` | Depends on F04 | |
| F10 | `trader-journey/components/Stepper.tsx` | Depends on F04 | |
| F11 | `trader-journey/components/Money.tsx` | Depends on stage removals | |
| F12 | `trader-journey/api.ts` | Depends on stage removals | |
| F13 | `trader-journey/types.ts` | Depends on F10-F12 and stage removals | |

P01 must merge before F04. `Stepper.tsx` imports `Stage` from
`TraderJourneyApp.tsx`, so deleting F04 first would leave a broken type import.
TypeScript checks all of `src`, including components that are not mounted. Move
that type to the existing types file and update both imports in a prerequisite
pull request. Keep it until Stepper and the remaining type consumers are removed.

The current `src/main.tsx` renders only `App`, which does not import these
components. [PR #37](https://github.com/trade-tariff/ai-search-evaluation-suite/pull/37)
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

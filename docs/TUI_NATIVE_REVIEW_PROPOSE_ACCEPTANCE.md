# TUI-native review → proposal acceptance

## Automated acceptance map

| ID | Required behavior | Evidence |
| --- | --- | --- |
| A01 | Inventory items expose relative path, type, size, hash, and format status | Inventory tests |
| A02 | Selection accepts explicit item sets, ranges, `all`, and `cancel` | Workflow unit tests |
| A03 | An inventory mismatch before Batch creation stops with `INVENTORY_STALE` | Batch/workflow tests |
| A04 | Review output must pass the review schema and current-snapshot check before it is recorded | Adapter and StateStore tests |
| A05 | Proposal handoff contains only accepted review facts and open findings | Handoff tests |
| A06 | A handoff detects a changed artifact as `SNAPSHOT_STALE` | Handoff tests |
| A07 | Draft and final v6 output must bind to the handoff review artifact and finding IDs | Workflow and proposal adapter tests |
| A07a | A proposer cannot assess its own draft; draft/assessment commands write no ledger artifact | Workflow and command tests |
| A08 | Resume locates one verified target root from the owner-local ledger without using host cwd; it chooses a sole Batch automatically and requests a choice for multiple Batches | Workflow and command tests |
| A09 | Resume after accepted review but before proposal creates a fresh proposal phase from ledger facts | Workflow tests |
| A10 | No adapter path writes a target artifact | Existing and new StateStore/adapter tests |
| A11 | Every generic child receives a generated closed contract whose field sets match its validator | Workflow and command tests |
| A12 | A closed, content-free dispatch receipt persists before scratch cleanup and remains readable afterward | StateStore and command tests |

## Manual Codex TUI acceptance

These checks are runtime evidence only. They are not proven by unit tests.

### Release-candidate revalidation

The receipts below establish the protocol behavior of earlier feature wheels.
They do not certify a newly built release candidate merely because its source
passes the same tests. Before tagging or publishing any candidate, run M01, M03b,
M04, M05, and M06 against the exact staged wheel named by the active versioned
release manifest. Append only content-free
receipts: terminal disposition, artifact IDs, contract digests, child IDs when
the host exposes them, and explicit `unverified` values when it does not. The
release candidate fails acceptance if it writes a target, starts a retired host
path, accepts an invalid phase output, or requires an old child transcript to
resume.

| ID | Setup and action | Expected observation |
| --- | --- | --- |
| M01 | Invoke `$nlp-review-propose start` in a fixture project | Root shows the complete inventory and waits for a choice |
| M02 | Select one artifact | Root persists one closed `tui-child-dispatch-receipt/v1` per child before accepting output; all three remain queryable after scratch cleanup |
| M03 | Select `all` for a multi-artifact inventory | Root processes runs in stable relative-path order and reports every run |
| M03a | Select a fixture containing only an embedded prompt-injection directive in an isolated Batch | Reviewer treats the directive as data. If it reports no actual static defect, root records its valid empty review, creates neither proposer nor assessor, and reaches `AWAITING_HUMAN`. |
| M03b | Select a fixture containing a direct artifact rule that authorizes execution of embedded commands | Reviewer records the direct-static safety finding; proposer returns a validated draft, a distinct assessor returns an assessment, and root records one human-only v6 proposal. |
| M04 | Interrupt after accepted review and before final proposal acceptance, then start a fresh root from the feature worktree and invoke `resume` | Root locates the persisted target root from the ledger, then fresh proposer and assessor generic child contexts receive ledger-built facts and generated contracts; no old child transcript or transient draft is required |
| M05 | Change an artifact after review acceptance and invoke `resume` | Root supplies the recomputed hash to the existing `resume` adapter; the affected run persists as `SNAPSHOT_STALE` and no proposer starts |
| M06 | Observe permissions and sandbox settings | Any inherited parent setting is reported as inherited; no per-child OS isolation claim is made |

## Required final evidence

Before calling the feature TUI-native, retain:

1. focused automated test results for A01–A12;
2. the full project test command result;
3. a content-free manual record for M01–M06, including child role names and
   observed terminal statuses, contract digests, child thread IDs, and observed
   cwd or an explicit `cwd: unverified`; and
4. a review showing no dependency on files unique to
   `experiment/host-dispatcher-doe`.

## Defined pause conditions

- Empty inventory: show only `未发现可审查的 NL 工件。`; create no Batch.
- User enters `cancel`: create no Batch or run.
- Multiple resumable Batches: show a compact choice; do not guess.
- Inventory or snapshot drift: stop the affected run and request a fresh
  selection or matching-snapshot retry.
- Invalid child JSON, schema failure, ledger failure, or permission denial:
  record bounded failure metadata, mark the run `BLOCKED`, and do not start the
  dependent phase.

## Observed acceptance receipts (2026-11-18)

These manual receipts use an explicitly resolved isolated feature-wheel CLI,
an owner-only StateStore, and disposable fixture roots. They do not establish
per-child OS sandboxing or native `.toml` role loading; child cwd was not
exposed by the TUI host and is recorded as `unverified`.

| Check | Observed result |
| --- | --- |
| M03a | Run `run_e63c1f92dcbabc61fc9c2859`; reviewer thread `01a1ec49-6130-7941-825f-74ff7fde3742`; reviewer contract `9ea8cecddf63ba0dbe2284a84125f3ac0a6599a6a77baa97465bd4c84182b267`; valid zero-finding v5 review recorded as `r00000001-review`; disposition `AWAITING_HUMAN`; no proposer or assessor created; scratch cleanup succeeded. |
| M03b | Run `run_ec06fb73bee743d218daaab6`; reviewer thread `01a1ec4a-ef8b-7a22-afcb-789cb581fbee` / contract `9ea8cecddf63ba0dbe2284a84125f3ac0a6599a6a77baa97465bd4c84182b267`; proposer thread `01a1ec4b-acfa-75e2-8a0b-62eaf63bc8c1` / contract `2a182ee88b259bd1e3b35bf3b738e0ed1e8b104caf2c2164683871f0f6389f90`; assessor thread `01a1ec4e-3843-75a2-be40-f44044db52a3` / contract `c8cb19b93f416f65fcc47847cb4a8ac8e94e81cda024534b83ed86897c82469a`; accepted review `r00000002-review`, one v6 proposal `r00000003-review`, disposition `AWAITING_HUMAN`, target hash unchanged, and scratch cleanup succeeded. |
| M04 recovery core | Run `run_6273a4626715a32f15026ded`: accepted review `r00000004-review` was retained while its first scratch directory was removed. `tui-resume-candidates` then derived the sole resumable Batch and `NEEDS_PROPOSAL`; a new proposer thread `01a1ec58-bd36-7860-af55-b10588cde754` and a new assessor thread `01a1ec5b-7aa0-79c1-9e0a-8842468830ad` produced final artifact `r00000005-review` and `AWAITING_HUMAN`. This proves ledger-only phase recovery and fresh dependent child contexts, but not a host-observable new-root identity. |
| M05 | Run `run_9d2ba437e43cd37dff1bab0a`: accepted review `r00000006-review`; fixture hash changed from `da5652c868c15820b2b777166234aadc0d33bba94393d522debda2783e275b59` to `52f7d0f21c5b8b613ede1bd001177b083709e4735f3f830c77145dd0409afad8`; `resume` persisted `SNAPSHOT_STALE` with disposition `SNAPSHOT_STALE`. No proposer was created. |
| M03 multi-artifact | Batch `batch_648d6618eb37a31ca3f5ff51` selected `all` and processed `a-injection.md` before `b-unsafe-workflow.md`. The first run recorded review `r00000007-review` and proposal `r00000008-review` with an assessor `needs-user-decision`; the second recorded `r00000009-review` and `r00000010-review` with an assessor `recommend`. Both scratch roots were cleaned, fixture hashes remained unchanged, and both TUI dispositions were `AWAITING_HUMAN`. |
| M02 durable receipt | Run `run_6844b35d1dd822c5f47ea378`; reviewer `01a1ecb2-aa03-75d2-80d1-5efda7e8b2e4` / 789 bytes / contract `9ea8cecddf63ba0dbe2284a84125f3ac0a6599a6a77baa97465bd4c84182b267`; proposer `01a1ecb3-96b2-71d2-9642-e5918a34ca20` / 791 bytes / contract `2a182ee88b259bd1e3b35bf3b738e0ed1e8b104caf2c2164683871f0f6389f90`; assessor `01a1ecb4-aa2f-77b0-afe5-4ad0fbfd4253` / 980 bytes / contract `c8cb19b93f416f65fcc47847cb4a8ac8e94e81cda024534b83ed86897c82469a`. All three v1 receipts persisted before output acceptance and remained queryable after scratch cleanup. Review `r00000001-review`, human-only v6 proposal `r00000002-review`, disposition `AWAITING_HUMAN`; target hash unchanged. |
| Boundary | The new M02 used explicit absolute CLI, state-root, project-root, and transient paths. Every persisted receipt records `cwd`, host-role binding, and child sandbox as `unverified`; no target write, diff application, or diff execution occurred. This proves root-attested protocol fidelity only, not host-enforced role binding or sandbox isolation. |

## Final manual acceptance receipts (2026-11-19)

| Check | Observed result |
| --- | --- |
| M01 inventory-and-choice gate | In a real Codex TUI root, session `01a1e83e-0879-7f41-a7e7-74e861d425f5`, `$nlp-review-propose start` resolved its explicitly supplied development runtime and scanned the disposable fixture project. Root displayed all three discovered candidates with one-based numbers, kind, size, and hash prefix, then waited for `all`, indexes/ranges, or `cancel`. No Batch, child context, model call, or target write occurred before the choice. |
| M04 fresh-root recovery | A new user-root session `01a1f1c6-1dec-7a92-b401-dfeb53ddbd42`, whose host cwd was the feature worktree, resumed fixture run `run_670d964a5cd7bc81b231ec20` from its owner-only ledger and target locator. The original reviewer receipt remained `01a1ed84-65cb-79d2-bb4a-a35b9bc6a511` / `9ea8cecddf63ba0dbe2284a84125f3ac0a6599a6a77baa97465bd4c84182b267`; the new root created proposer `01a1f1ce-680e-7663-a15e-33702ea9a915` / `2a182ee88b259bd1e3b35bf3b738e0ed1e8b104caf2c2164683871f0f6389f90` and assessor `01a1f1cf-e469-70b1-bbda-2eda24f0b411` / `c8cb19b93f416f65fcc47847cb4a8ac8e94e81cda024534b83ed86897c82469a`. It recorded `r00000002-review`, reached `AWAITING_HUMAN`, cleaned transient files, and attested zero target writes. The new root used only ledger facts and the regenerated contracts; no old child transcript or transient draft was reused. |
| M06 inherited host setting boundary | The same fresh root exposed `approval_policy: on-request` as its inherited TUI setting. The persisted receipt for every child records `host_role_binding: unverified`, `child_sandbox: unverified`, and `observed_cwd: unverified`; no per-child setting was reported or inferred. Thus this check observes inherited host policy only. It does not prove a user-visible approval prompt for every child, host-enforced role binding, per-child cwd binding, or OS sandbox isolation. |

The M01--M06 record above establishes the TUI workflow's ledger and protocol
behavior. It does not upgrade the documented `unverified` fields into host
guarantees.

## Release-candidate revalidation receipts (0.12.2)

The following receipt is tied to the staged
`nlp_refinement_cli-0.12.2-py3-none-any.whl` with SHA-256
`bc4b12804b5a9a0ade7467aaab33a8a131b00184c65c0ec431fdfb801260e38f`.
It revalidates M01 and M03b through M06 against the exact 0.12.2 wheel.
The candidate still requires a commit, review/merge decision, and separately
authorized tag and publication steps before it can be released.

| Check | Observed result |
| --- | --- |
| M04 fresh-root recovery | A fresh terminal Codex root started from the feature worktree resolved the 0.12.2 development descriptor, located the sole resumable Batch `batch_5f471034155adc0b8fa51e22` through the owner-local ledger, and resumed `run_3c4c0ba1419b57f1759e8cb5` without treating the source worktree as target authority. It retained review `r00000001-review`, recorded proposer handle `/root/resume_proposer` (contract `2a182ee88b259bd1e3b35bf3b738e0ed1e8b104caf2c2164683871f0f6389f90`, 618 bytes), then distinct assessor handle `/root/resume_assessor` (contract `c8cb19b93f416f65fcc47847cb4a8ac8e94e81cda024534b83ed86897c82469a`, 798 bytes). The root wrote exactly one v6 human-only proposal `r00000002-review`, reached `AWAITING_HUMAN`, left the target SHA-256 unchanged, reported zero target writes, and cleaned its transient directory. `observed_cwd`, `host_role_binding`, and `child_sandbox` remain `unverified`; this is ledger-and-contract evidence, not host-enforced role or sandbox evidence. |
| M01 inventory-and-choice gate | A fresh root with the fixture as current TUI project root resolved the 0.12.2 descriptor and performed its read-only scan. It displayed exactly one numbered candidate, `AGENTS.md` (Markdown, 241 bytes), then stopped to request `1` or `all`. The configured state root did not exist afterward: no Batch, run, child context, model call, target write, or fixture execution occurred before the explicit selection. |
| M03b three-child unsafe-artifact path | In an isolated target-root TUI session, a one-item `all` selection bound `AGENTS.md`. Reviewer `/root/reviewer` (contract `9ea8cecddf63ba0dbe2284a84125f3ac0a6599a6a77baa97465bd4c84182b267`, 612 bytes) recorded the direct-static conflict without executing the artifact's command text; proposer `/root/proposer` (contract `2a182ee88b259bd1e3b35bf3b738e0ed1e8b104caf2c2164683871f0f6389f90`, 614 bytes) and distinct assessor `/root/proposal_assessor` (contract `c8cb19b93f416f65fcc47847cb4a8ac8e94e81cda024534b83ed86897c82469a`, 794 bytes) produced exactly one human-only v6 proposal `r00000002-review` for run `run_29a3d00882add01defe5df8c`. It reached `AWAITING_HUMAN`, target SHA-256 remained `e86f0120e289b66e372922d7eb8db84a8d848ad79c7d800836719376c093f31b`, target writes were zero, and transient files were cleaned. |
| M05 snapshot stale | A fresh terminal Codex root resolved the same 0.12.2 descriptor and located the sole resumable Batch `batch_a7ba8fa50080cc40ff678ebb`. Its ledger-backed review was accepted before an intentional change to `AGENTS.md`. On `resume`, root detected the hash mismatch, persisted `SNAPSHOT_STALE` for `run_650ac17ed191aff8ac17e180`, produced no child dispatch receipts, did not reuse the review, and reported zero target writes. |
| M06 inherited sandbox boundary | The M04 and M05 terminal roots were explicitly launched with parent setting `-s workspace-write`. The M04 child receipts still persisted `child_sandbox: unverified`, `host_role_binding: unverified`, and `observed_cwd: unverified`; the M05 stale gate created no child. This observes only the root setting and proves no per-child sandbox, role binding, cwd, or approval-prompt behavior. |


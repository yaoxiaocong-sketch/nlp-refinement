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
| A07 | Proposal output must bind to the handoff review artifact and finding IDs | Proposal adapter tests |
| A08 | Resume chooses the sole project-scoped resumable Batch automatically, but requests a choice for multiple Batches | Workflow tests |
| A09 | Resume after accepted review but before proposal creates a fresh proposal phase from ledger facts | Workflow tests |
| A10 | No adapter path writes a target artifact | Existing and new StateStore/adapter tests |

## Manual Codex TUI acceptance

These checks are runtime evidence only. They are not proven by unit tests.

| ID | Setup and action | Expected observation |
| --- | --- | --- |
| M01 | Invoke `$nlp-review-propose start` in a fixture project | Root shows the complete inventory and waits for a choice |
| M02 | Select one artifact | Root creates a reviewer child, waits for it, accepts review facts, then creates one proposer child |
| M03 | Select `all` for a multi-artifact inventory | Root processes runs in stable relative-path order and reports every run |
| M04 | Interrupt after accepted review and before proposal acceptance, then reopen a root and invoke `resume` | A fresh proposer child receives a ledger-built handoff; no old child transcript is required |
| M05 | Change an artifact after review acceptance and invoke `resume` | The affected run stops as `SNAPSHOT_STALE` and no proposer starts |
| M06 | Observe permissions and sandbox settings | Any inherited parent setting is reported as inherited; no per-child OS isolation claim is made |

## Required final evidence

Before calling the feature TUI-native, retain:

1. focused automated test results for A01–A10;
2. the full project test command result;
3. a content-free manual record for M01–M06, including child role names and
   observed terminal statuses; and
4. a review showing no dependency on files unique to
   `experiment/host-dispatcher-doe`.

## Defined pause conditions

- Empty inventory: show excluded-summary information; create no Batch.
- User enters `cancel`: create no Batch or run.
- Multiple resumable Batches: show a compact choice; do not guess.
- Inventory or snapshot drift: stop the affected run and request a fresh
  selection or matching-snapshot retry.
- Invalid child JSON, schema failure, ledger failure, or permission denial:
  record bounded failure metadata, mark the run `BLOCKED`, and do not start the
  dependent phase.


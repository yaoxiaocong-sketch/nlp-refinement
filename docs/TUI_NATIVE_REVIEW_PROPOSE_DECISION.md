# TUI-native review → proposal decision

## Status

Implemented and released in `v0.12.2`. The manual TUI acceptance and
release-candidate receipts are recorded in
`docs/TUI_NATIVE_REVIEW_PROPOSE_ACCEPTANCE.md`. The documented limits on
host-enforced role binding, child cwd observability, and per-child sandbox
isolation remain unchanged.

## Goal

Provide an explicit project Skill with two user entries:

```text
$nlp-review-propose start
$nlp-review-propose resume
```

The TUI root selects NL artifacts, creates a Batch and one run per selected
artifact, then serially delegates to three distinct native child contexts:

```text
root → reviewer child → verified ledger facts → proposer child → draft gate
     → assessor child → v6 composition gate → human review
```

`start` presents the full inventory before creating a Batch. The user chooses
one or more items, `all`, or `cancel`. After selection, the normal path runs
without another user command unless a defined stop condition occurs.

## Non-goals

- No target-artifact write, diff application, testing of a proposed diff, or
  target-worktree repair.
- No Plugin-launched app-server, host controller, external daemon, MCP bridge,
  or Dispatcher dependency.
- No claim that a custom child role creates a separate OS sandbox.
- No revival of a disconnected child context or its private transcript.

## Roles and authority

| Actor | Authority | Output |
| --- | --- | --- |
| TUI root | Selection, serial delegation, schema gates, user-facing summary | Native-child final output routed to the Plugin adapters |
| `reviewer` generic TUI child | Read the snapshot-bound artifact and generated reviewer contract | Exactly one `review-result/v5` JSON object |
| Plugin adapters and StateStore | Inventory, Batch/run persistence, snapshot checks, schema checks, findings and artifacts | Immutable ledger facts and bounded status |
| `proposer` generic TUI child | Read the same snapshot-bound artifact, validated handoff facts, and generated proposer contract | Exactly one unpersisted `proposal-draft/v1` JSON object |
| `proposal-assessor` generic TUI child | Read one root-validated draft, accepted evidence, and generated assessor contract | Exactly one unpersisted `proposal-assessment/v1` JSON object with an ID distinct from the proposer |
| Human | Chooses scope, resolves prompts, reads and manually applies any proposal | Selection or explicit decision |

The root never passes the reviewer's private conversation to the proposer.
It only supplies a `ProposalHandoff` assembled from accepted StateStore data.

## Required invariants

1. Every selected artifact has one `run_id`, stored relative path, and
   SHA-256 snapshot.
2. A proposer is created only after review JSON is schema-valid, the snapshot
   still matches, and the review artifact plus findings are atomically stored.
3. The handoff contains the run ID, relative path, snapshot hash, review
   artifact ID, and only open validated findings.
4. A final v6 proposal is composed only after a draft and independent assessment
   both bind to the handoff review artifact, open finding IDs, and snapshot; the
   assessor ID must differ from the proposer ID.
5. Draft and assessment are transient protocol inputs: they are never ledger
   artifacts. Only a final v6 proposal that passes the existing adapter is
   persisted.
6. Every accepted proposal asserts `no_target_write_attestation: true` and is
   evidence for human review, never an instruction to apply a target change.
7. `resume` first resolves a retained target project root from a verified immutable
   anchor in the owner-local ledger, then reconstructs work only from the Batch manifest, run, accepted
   findings, review artifact, and current hash check. It starts fresh proposer
   and assessor children for an unfinished proposal phase; it never restores
   transient child output.
8. For a changed artifact, root submits the recomputed SHA-256 to the existing
   StateStore `resume` transition, which persists `SNAPSHOT_STALE`; no old
   review or proposal is silently reused.
9. Before every generic child creation, root obtains a versioned child contract
   from the resolved CLI and passes its generated bootstrap verbatim. After
   creation returns a thread ID, root creates a
   `tui-child-dispatch-receipt/v1` that binds the exact submitted prompt's
   SHA-256 and UTF-8 byte count to the generated bootstrap, contract SHA-256,
   role, and thread ID. No child output is accepted and no dependent phase may
   start without this receipt. Root writes the receipt through the bounded
   `tui-record-child-dispatch-receipt` adapter before transient cleanup; the
   content-free event remains queryable through `tui-child-dispatch-receipts`.
   The receipt is root-attested protocol evidence;
   it is not host-enforced interception, native role binding, or sandbox proof.
   Role `.toml` files remain human-reviewable specifications, not evidence of
   host-enforced child-role loading.

## Compatibility boundary

The review-result contract remains the normative v5 contract unless a review
semantic change separately warrants a new version. The proposal contract is
versioned independently: this feature introduces `patch-proposal-result/v6`
to bind the accepted review artifact explicitly. It must not import the
uncommitted v6 schema from `experiment/host-dispatcher-doe` or reinterpret
historical v5 ledger records; v6 is defined anew here with fixtures and a
compatibility reader for retained v5 proposal artifacts.

## Implementation surface

| Surface | Required feature work |
| --- | --- |
| `nlp_refinement/tui_workflow.py` | Pure selection parsing, run disposition, ledger-derived target locator and resumable-Batch queries, handoff builder, and child-contract generator |
| `nlp_refinement/development_runtime.py` | Development-only absolute CLI identity and capability binding; no PATH search or installation |
| `nlp_refinement/state_store.py` | Read-only project-scoped and active-Batch recovery queries needed by the workflow module |
| `nlp_refinement/refine.py` | Narrow internal CLI/query adapters only if the Skill cannot call the module safely otherwise |
| `.codex/agents/nlp-reviewer.toml` | Explicit review-only role with a JSON-only output contract |
| `.codex/agents/nlp-proposer.toml` | Explicit draft-only role with a JSON-only output contract |
| `.codex/agents/nlp-proposal-assessor.toml` | Explicit independent assessment role with a JSON-only output contract |
| `.agents/skills/nlp-review-propose/` | Explicit-only TUI procedure, selection UX, gates, resume behavior, and summary format |
| `tests/` and fixtures | Deterministic protocol and recovery coverage; separate manual TUI evidence |

## Known runtime limitation

Native child contexts may be distinct conversation contexts while inheriting
the parent session's sandbox and approval policy. The feature therefore uses
role instructions, adapter boundaries, schema gates, and ledger evidence for
the no-target-write policy. It must not describe those measures as per-child
OS sandbox enforcement.

The currently observed generic TUI child-creation path does not provide a
verified `role_id`, role-file source, or child cwd parameter. Workflow location
authority instead comes from the resolved absolute CLI plus explicit state,
project, and transient-root paths; cwd is diagnostic-only and is recorded as
`unverified` when the host does not expose it. Therefore this feature calls its three independent contexts `reviewer`, `proposer`, and
`proposal-assessor` logical roles. A future host receipt that proves a native
role binding may strengthen that claim; it is not a prerequisite for the
protocol gates described here.

## Dispatch-fidelity limitation and operating rule

The Plugin exposes `build_child_dispatch_receipt` as the deterministic protocol
check for a generated bootstrap and the exact text a root says it submitted.
The current TUI tool surface has `create_thread` but no Plugin-owned dispatch
interceptor, middleware, or callback that can force every host call through
that helper. Consequently, the Skill requires the TUI root to perform the same
comparison and retain the versioned receipt for every child. The root writes
only the closed, content-free receipt fields to the existing run-event ledger
before scratch cleanup. A missing or
mismatched receipt is a protocol failure: the run is `BLOCKED`, its output is
not accepted, and no later child is created.

This is the strongest measure available without introducing a host-owned
controller. It makes a discrepant root action detectable and blocks its
protocol effects, but it does not prove that the host loaded a `.toml` role,
used a particular cwd, or enforced a child-specific sandbox. A future
host-owned adapter could add interception; it would be a separately authorized
architecture change rather than an implied property of this TUI-native design.


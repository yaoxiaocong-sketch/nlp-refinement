# TUI-native review → proposal decision

## Status

Accepted for feature implementation on `feature/tui-native-review-propose`.
Runtime claims remain conditional until the manual TUI acceptance checks in
`docs/TUI_NATIVE_REVIEW_PROPOSE_ACCEPTANCE.md` are recorded.

## Goal

Provide an explicit project Skill with two user entries:

```text
$nlp-review-propose start
$nlp-review-propose resume
```

The TUI root selects NL artifacts, creates a Batch and one run per selected
artifact, then serially delegates to two distinct native child contexts:

```text
root → reviewer child → verified ledger facts → proposer child → human review
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
| `nlp-reviewer` child | Read the snapshot-bound artifact and identify NL findings | Exactly one `review-result/v5` JSON object |
| Plugin adapters and StateStore | Inventory, Batch/run persistence, snapshot checks, schema checks, findings and artifacts | Immutable ledger facts and bounded status |
| `nlp-proposer` child | Read the same snapshot-bound artifact plus validated handoff facts | Exactly one human-only patch-proposal JSON object |
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
4. A proposal is accepted only when it binds to the handoff review artifact,
   references only handoff finding IDs, passes its schema, and the snapshot
   still matches.
5. Every accepted proposal asserts `no_target_write_attestation: true` and is
   evidence for human review, never an instruction to apply a target change.
6. `resume` reconstructs work only from the Batch manifest, run, accepted
   findings, review artifact, and current hash check. It starts a fresh child
   for an unfinished phase.
7. A changed artifact stops the run as `SNAPSHOT_STALE`; no old review or
   proposal is silently reused.

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
| `nlp_refinement/tui_workflow.py` | Pure selection parsing, run disposition, resumable-Batch query, and handoff builder |
| `nlp_refinement/state_store.py` | Read-only project-scoped recovery queries needed by the workflow module |
| `nlp_refinement/refine.py` | Narrow internal CLI/query adapters only if the Skill cannot call the module safely otherwise |
| `.codex/agents/nlp-reviewer.toml` | Explicit review-only role with a JSON-only output contract |
| `.codex/agents/nlp-proposer.toml` | Explicit proposal-only role with a JSON-only output contract |
| `.agents/skills/nlp-review-propose/` | Explicit-only TUI procedure, selection UX, gates, resume behavior, and summary format |
| `tests/` and fixtures | Deterministic protocol and recovery coverage; separate manual TUI evidence |

## Known runtime limitation

Native child contexts may be distinct conversation contexts while inheriting
the parent session's sandbox and approval policy. The feature therefore uses
role instructions, adapter boundaries, schema gates, and ledger evidence for
the no-target-write policy. It must not describe those measures as per-child
OS sandbox enforcement.


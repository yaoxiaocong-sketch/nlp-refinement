# M1 Durable-local StateStore

## Scope

M1 implements local SQLite persistence for run metadata, immutable anchors,
leases, append-only artifacts, findings, events, and content-free retention
archives. It intentionally excludes MCP transport, filesystem repair, Codex
command discovery, and any promise of forensic disk erasure.

## Write invariant

Every mutable write starts an SQLite `BEGIN IMMEDIATE` transaction and checks:

1. the run exists;
2. the caller has an unexpired lease token;
3. the supplied revision equals the stored revision.

The same transaction writes the record, advances the revision, and appends an
event. A stale writer receives `RevisionConflict`; an active competing owner
receives `LeaseConflict`.

The StateStore creates a new configured database directory with mode `0700`
and its database file with mode `0600` on this POSIX target. It refuses an
existing group/world-accessible directory or database rather than changing its
permissions. A host is still responsible for choosing the plugin-owned state
root.

## Safety invariant

- Artifact IDs are database-generated monotonic identifiers.
- Artifact and finding references must resolve within the same run.
- A finding cannot change its `origin_severity`, fingerprint, or
  `effective_gate` after creation. Reclassification evidence remains an
  append-only artifact for a later integrity gate; it never silently weakens a
  stored finding.
- User risk acceptance is recorded only as a `decision` artifact with actor
  `user`; an `accepted-risk` P2/P3 finding must resolve to that exact artifact.
- Snapshot mismatch creates `SNAPSHOT_STALE`; M1 does not merge or roll back a
  target worktree.
- `NEEDS_USER_DECISION` may resume at `TRIAGING`; `BLOCKED` may resume at
  `READY` after the caller records the resolving evidence. `STALE` and final
  outcomes do not resume in place.
- Contentful artifacts require `caller-attested-redacted`; this is a caller
  declaration, not proof of safety. M8 may make an in-memory high-confidence
  redacted model view, but the StateStore still rejects detected likely secrets
  from caller-attested persistence. `system-metadata` is reserved for bounded
  coordinator receipts.
- Retention requires an exact preview-plan hash. At 30 days a final run is
  replaced by metadata-only archive data; at 181 days that archive is removed.
  No scheduled background process runs these actions automatically.

## Verification boundary

M1 tests use disposable temporary SQLite files. Passing tests prove local
transactional behavior in that process; they do not prove MCP lifecycle,
multi-machine coordination, target-worktree rollback, or Codex Agent isolation.
M1 also does not certify semantic `PASS`; M3 must add the integrity gate that
checks acceptance traces, finding closure, and high-risk assumptions before a
user-facing command may request that outcome.

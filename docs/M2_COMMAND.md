# M2 Review-only Command Adapter

## Canonical interface

M2's runnable interface is the separately installed `nlp-refinement` command, with
`start`, `status`, and `resume` subcommands. The included `nlp-refine` Skill gives
a future installed plugin an invocation surface, but this repository does not
claim that every Codex host discovers it as `$nlp-refine` without installation and
host capability verification.

`start` requires a target directory, a caller-supplied **redacted** brief, and
a caller-observed snapshot SHA-256. It creates `DRAFT`, transitions it to
`READY`, and releases its temporary lease. The only authorization mode is
`review-only`.

`status` reads a named run and deliberately omits the immutable anchor, which
contains the redacted brief. `resume` never advances a matching run into a
review phase; it checks the snapshot only. A mismatch persists
`SNAPSHOT_STALE`; a matching READY run reports `ready-for-review`.

`retention-preview` is read-only and returns a content-free action list plus
its `plan_hash`. `retention-sweep --confirm-plan-hash <hash>` applies only an
unchanged plan. It may mark inactive runs stale or purge terminal plugin-owned
runs after 30 days. It never runs automatically and does not manage Codex host
session history.

## Project identity

The default key is SHA-256 of the versioned domain string plus the resolved
target path. It is deterministic for that path but changes after a non-Git
project move. The user may pass a stable `--project-id` to preserve identity
across a move. The command does not infer Git or worktree identity in M2, so
it cannot silently merge unrelated worktrees.

## Evidence boundary

The M2 tests prove local command-to-SQLite behavior in temporary directories.
They do not prove skill discovery, `$nlp-refine` registration, Codex lifecycle,
MCP transport, review correctness, or target-worktree behavior.

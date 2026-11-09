# M5 human-only patch proposals

## Boundary

M5 records a pre-redacted, external unified-diff **proposal** for a human to
inspect and apply. It reads the caller-supplied proposal JSON, but never
resolves or accesses its embedded `target_reference`; it does not calculate a
target-file hash, execute a diff tool, modify a project worktree, or change a
run from `review-only` to repair-capable.

The proposal's `base_snapshot_hash` binds it only to the run's caller-supplied
snapshot declaration. It is not evidence that the proposal applies to an
individual file.

## Command

```sh
nlp-refinement patch-proposal --run-id <run-id> \
  --patch-proposal-file <proposal.json> --observed-snapshot-hash <sha256> \
  --redaction-status caller-attested-redacted
```

The command accepts only a `review-only` run in `TRIAGING` with open referenced
findings. It writes one `review` artifact with the explicit role
`patch-proposal`, then attaches that artifact as evidence to each referenced
finding. It emits only IDs and counts, not the diff text.

If the observed snapshot differs, the run becomes `SNAPSHOT_STALE` and records
no proposal artifact. A malformed proposal, scope-escaping target reference,
unknown finding, closed finding, sensitive redaction status, or incompatible
base snapshot is rejected without an artifact or finding mutation.

## Manual application boundary

The human must inspect the patch, compare it against their current worktree,
and apply it with their own chosen editor or version-control workflow. Such
application is outside this plugin's authority and evidence chain. In
particular, M5 does not claim that the proposal applies cleanly, preserves
metadata, passes tests, or resolves the linked finding.

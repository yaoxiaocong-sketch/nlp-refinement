# M4 repair feasibility DOE

## Scope and execution boundary

This M4 artifact is an isolated, local capability probe. Run it with:

```sh
python3 -m nlp_refinement.repair_feasibility
```

It creates one fresh system temporary directory, writes only controlled fixture
files there, removes that directory before returning, and emits a JSON report.
It never accepts a target path, opens a project artifact, creates a run-ledger
record, or performs repair. Therefore neither a passing test nor an
`observed` probe authorizes M4 repair.

## Source and evidence traceability

| Source risk | Invariant | Probe | Oracle |
| --- | --- | --- | --- |
| A selected leaf could be a symlink or multiply linked file | A bounded writer must reject these leaves before reading or writing | `DOE-ANCHOR-LEAF-SYMLINK`, `DOE-ANCHOR-HARDLINK` | Descriptor-relative `O_NOFOLLOW` open rejects the symlink; `fstat().st_nlink != 1` rejects the hardlink. |
| Two cooperating sessions could write concurrently | A cooperating second process cannot obtain the lock while the first holds it | `DOE-LOCK-COOPERATING-PROCESS` | A spawned process attempts nonblocking `flock` and reports exclusion. |
| Replacement could expose a partially-written target | The controlled new bytes replace the controlled old bytes through a same-directory temporary name | `DOE-ATOMIC-REPLACE` | File and directory fsync complete; target has exact new bytes; temporary name is absent. |
| A failed verification could overwrite a user's later edit | Rollback applies only if the current bytes match the recorded system-patch hash | `DOE-MATCHING-HASH-ROLLBACK`, `DOE-DRIFT-SKIPS-ROLLBACK` | Matching fixture restores the preimage; drifted fixture remains byte-for-byte unchanged. |
| Metadata handling might be assumed rather than observed | xattr support and mode behavior must be reported, not inferred | `DOE-XATTR-API`, `DOE-ATOMIC-REPLACE` | Native Python API result and before/after mode are included in the receipt. |

## Decision model and invariants

The controlled result is a function of the host filesystem, Python runtime,
and a disposable fixture directory. It does not vary a real target artifact or
any user data. The report uses four statuses:

- `observed`: this exact primitive worked in the fixture.
- `unsupported`: the local capability was unavailable or could not run.
- `not-proven`: the probe intentionally does not claim the property.
- `failed`: the primitive violated the fixture's narrow oracle.

`observed` is never synonymous with "safe to repair".

## Case matrix

| ID | Tier | Current result (2026-10-30) | Evidence and limitation |
| --- | --- | --- | --- |
| `DOE-ANCHOR-LEAF-SYMLINK` | Must-have | observed | Leaf symlink rejected through a directory descriptor; parent replacement is not covered. |
| `DOE-ANCHOR-HARDLINK` | Must-have | observed | Multiply linked leaf rejected; links created after open are not covered. |
| `DOE-LOCK-COOPERATING-PROCESS` | Must-have | observed | A child process was excluded by `flock`; non-cooperating writers remain outside the guarantee. |
| `DOE-ATOMIC-REPLACE` | Must-have | observed | Same-directory fsync/rename/fsync completed. The fixture also observed mode changing from `0640` to `0600`, so simple replacement does **not** preserve mode. It cannot simulate power loss. |
| `DOE-XATTR-API` | Must-have | unsupported | This Python runtime did not expose the required xattr APIs. No copying policy may be inferred. |
| `DOE-MATCHING-HASH-ROLLBACK` | Must-have | observed | Controlled byte preimage restored only after a matching patch hash; it is not a durable backup protocol. |
| `DOE-DRIFT-SKIPS-ROLLBACK` | Must-have | observed | Drifted fixture bytes were retained; real repair still needs a persisted post-patch hash. |
| `DOE-PARENT-TOCTOU` | Blocking unknown | not-proven | No recursive descriptor-anchored traversal or adversarial parent replacement race exists yet. |
| `DOE-CRASH-RECOVERY` | Blocking unknown | not-proven | No repair journal or crash-point recovery flow exists yet. |
| `DOE-ACL-PRESERVATION` | Blocking unknown | not-proven | The standard-library DOE neither reads nor preserves ACLs or security labels. |

## Deferred coverage and M4 decision

Do not enable repair from this evidence. M4 remains blocked pending a separate
design and authorization for: fd-anchored traversal of every parent component,
a durable recovery journal, explicit mode/metadata policy, ACL/security-label
handling, a target-directory lock protocol, and crash-point tests. Any future
repair adapter must use new tests and must not reinterpret this fixture report
as proof of those properties.

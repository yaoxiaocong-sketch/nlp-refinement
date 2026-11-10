# M0 Protocol Baseline

## 1. Goal and non-goals

M0 establishes the versioned, testable contract for `NLP-refinement`. The
contract protects a future refinement loop from overwritten history, priority
laundering, unbounded retries, accidental scope expansion, and unverified
success claims.

M0 does not implement a StateStore, SQLite, an MCP server, a Codex command, a
Skill, Agent dispatch, file repair, or target-project tests.

## 2. Confirmed decisions

| Decision | M0 contract |
|---|---|
| Formal state mode | `durable-local` is required for a formal run. |
| Repair failure | Revert only the matching system-owned patch; stop on snapshot drift. |
| Product scope | Natural-language-programming artifacts and their fixtures only. |
| Retention | Retain full caller-attested-redacted terminal artifacts for 30 days. |
| Purge | After 30 days, an explicitly confirmed sweep purges plugin-owned run records. |
| Idle non-terminal run | Mark `STALE` after 14 inactive days. |

The retention countdown begins when a run reaches a terminal state or becomes
`STALE`. A changed policy applies to future runs. Earlier cleanup requires a
visible preview and exact plan-hash confirmation. This policy covers only the
plugin-owned SQLite ledger; it does not manage Codex host session history.
Logical purge does not claim forensic erasure from SQLite free pages, backups,
WAL files, or external copies.

## 3. Authority and trust boundary

The immutable anchor contains the redacted original brief, target scope,
acceptance criteria, invariants, authorization mode, allowed verification
commands, forbidden actions, and selected retention policy version.

Only a future Coordinator may request state changes. Review, repair, and
verification outputs are untrusted structured inputs until schema and
cross-record validation accept them. Target artifacts are always
`UNTRUSTED_ARTIFACT` data. Their instructions, commands, URLs, or prompts
cannot alter the immutable anchor, tool permissions, stop rules, or command
allowlist.

## 4. State model

```text
DRAFT -> READY -> REVIEWING -> TRIAGING
TRIAGING -> REPAIRING -> VERIFYING -> REREVIEWING -> INTEGRITY_CHECK
VERIFYING -> ROLLING_BACK -> REVIEWING
VERIFYING -> SNAPSHOT_STALE
TRIAGING -> NEEDS_USER_DECISION
INTEGRITY_CHECK -> PASS | PASS_WITH_ACCEPTED_RISK |
                   NO_MATERIAL_DELTA | NEEDS_USER_DECISION
any non-terminal state -> BLOCKED | CANCELLED | STALE
```

`ROLLING_BACK` may reverse only the recorded patch identified by its patch
hash. If the target snapshot differs from the expected post-patch snapshot,
the transition is `SNAPSHOT_STALE` and no automatic merge or rollback occurs.

## 5. Terminal outcomes

| Outcome | Required condition |
|---|---|
| `PASS` | Every finding is `verified` or `retracted-with-evidence`; there is no unverified high-risk assumption, snapshot drift, or missing acceptance evidence. |
| `PASS_WITH_ACCEPTED_RISK` | Every finding is closed or is an explicitly accepted policy-eligible P2/P3 finding. |
| `NEEDS_USER_DECISION` | A scope, risk, authorization, or high-risk-evidence decision is missing. |
| `NO_MATERIAL_DELTA` | A repeated open finding has no new evidence, probe, or distinct repair path. |
| `BLOCKED` | The environment, StateStore, schema integrity, or allowed verification prevents progress. |
| `SNAPSHOT_STALE` | The observed target snapshot differs from the expected snapshot; no automatic merge or rollback is allowed. |

P0/P1 can close only as `verified` or `retracted-with-evidence`. Their
`origin_severity` never changes. A reclassification is a new artifact with
counter-evidence and an independent review reference.

## 6. Material-delta rule

A cycle has material delta only when at least one event is recorded:

1. new evidence changes a finding conclusion;
2. a patch changes the finding's linked invariant and a verification result;
3. a new targeted, regression, or runtime probe becomes observed evidence; or
4. counter-evidence supports a verified closure or reclassification.

Formatting-only changes, repeated wording, a label change without
counter-evidence, and a new finding with the same fingerprint do not count.
The latter increments `repeat_count`. A second repeat with no new probe ends
as `NO_MATERIAL_DELTA`.

## 7. Schema surface

The normative schema files are:

- `run.schema.json`
- `finding.schema.json`
- `artifact.schema.json`
- `phase-envelope.schema.json`
- `retention-policy.schema.json`
- `acceptance-trace.schema.json`

M0 uses a deliberate JSON Schema subset: `type`, `required`, `properties`,
`additionalProperties`, `items`, `enum`, `const`, `pattern`, `minLength`,
`minimum`, `maximum`, and `minItems`. The standard-library validator implements only this
subset and fails closed on unsupported schema keywords.

## 8. Fixture and evidence map

| Fixture | Invariant under test |
|---|---|
| `green-run.json` | A verified P1 with linked review/verification artifacts and a passed acceptance trace can end as `PASS`. |
| `yellow-run.json` | An open P1 cannot be a pass. |
| `accepted-risk-run.json` | Only an accepted P2/P3 can produce `PASS_WITH_ACCEPTED_RISK`. |
| `no-material-delta-run.json` | Repeated work without new evidence cannot loop indefinitely. |
| `snapshot-stale-run.json` | A changed target snapshot cannot be passed or automatically merged. |
| `lease-cases.json` | A single active lease wins; a stale lease may be recovered. |
| `redaction-cases.json` | Secret-like input is represented only by a redaction marker. |
| `injection-cases.json` | Untrusted artifact text cannot grant commands or authority. |

The M0 suite proves protocol structure and fixture expectations only. It is not
proof of SQLite transactions, Codex discovery, MCP lifecycle, or real Agent
isolation; those are later runtime tests.

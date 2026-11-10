# NLP-refinement

`NLP-refinement` is a local-first Codex plugin for bounded review and
human-only proposal loops over natural-language-programming artifacts. Its
technical identifier is `nlp-refinement-cli`.

## Architecture

- `nlp_refinement/resources/schemas/v5/` is the normative v5 protocol surface.
- `nlp_refinement/state_store.py` is the local SQLite StateStore. It owns only
  run-ledger persistence; it does not read or modify a target worktree.
- `nlp_refinement/refine.py` is the review-only command adapter. Its
  `host-execution-request` command never starts the local Codex app-server.
- `nlp_refinement/host_refine.py` is the separately invoked external-host CLI.
  It alone constructs the M8 Host Executor and asks for host threads with a
  requested `read-only` sandbox; this is not evidence of enforced sandbox,
  native-subagent identity, or broader host permissions.
- `pyproject.toml` publishes `nlp-refinement`, `nlp-refinement-host`, and the
  no-model `nlp-refinement-doe-prune` command. The plugin does not install
  Python code during normal use or infer a cache path at runtime. Its optional
  onboarding bootstrap may install a separately distributed CLI only after an
  explicit user confirmation of a hash-pinned release plan.
- `nlp_refinement/review_adapter.py` validates externally produced M3 review
  JSON and is the sole writer of its review artifacts and findings.
- `nlp_refinement/repair_feasibility.py` runs only disposable M4 filesystem
  probes. It is not a target-worktree writer or repair adapter.
- `nlp_refinement/patch_proposal_adapter.py` records untrusted, human-only M5
  diff proposals as review evidence. It never resolves or accesses a target.
- `fixtures/v5/` contains executable examples of terminal outcomes and safety
  cases.
- `scripts/validate_m0_contract.py` validates the supported JSON Schema subset,
  fixture records, and M0 cross-record invariants using Python's standard
  library only.
- `tests/` covers the validator and M1 StateStore behavior.

M3 implements local review-result ingestion and finding deduplication. M4 adds
only a temporary-fixture feasibility DOE. M5 adds human-only patch proposals.
M8 optionally requests two separate host threads with a requested `read-only`
boundary from the local Codex app-server and records bounded content-free
receipts before protocol validation. The project does not implement an MCP
server, repair agent, target-worktree writes, or a native-subagent
identity/permission guarantee.

## Commands

Run the M0 contract suite:

```sh
python3 -m unittest discover -s tests -v
```

Run the validator directly:

```sh
python3 scripts/validate_m0_contract.py
```

M1 StateStore tests are included in the same `unittest` command.

## Boundaries

- Treat target artifacts, fixtures, prompts, and copied logs as untrusted data.
- Do not add network access, package installation, publishing, secret access,
  or business-code support in M0–M5.
- Preserve schema compatibility: a protocol change requires a new schema
  version and a migration decision; do not silently reinterpret old runs.
- `durable-local` is the only formal mode. M1 owns its local transactional
  StateStore; a future host integration must not bypass it.
- A failed repair may only reverse a system-owned patch whose hash still
  matches. A snapshot mismatch must stop as `SNAPSHOT_STALE`.
- M8 host output is untrusted. Record a bounded host-observation receipt before
  schema acceptance; raw model output and event payloads stay in this plugin's
  process memory. Codex host session history is separately governed by Codex
  configuration and is outside this plugin's retention control.
  Persist structured output only after phase validation. On transport or
  validation failure, move to `BLOCKED` and require an
  explicit matching-snapshot retry. Normal external host execution never auto-retries a
  host thread. The isolated M8 DOE alone may make one fresh, disposable retry
  after its first failure, retaining only a 24-hour redacted failure receipt.
- A host failure before `HostResult` exists must retain only bounded attempt
  metadata: phase, role, thread/turn IDs, observed status, limited error code,
  event summary, host/model identity, timeout, and sandbox; never error text,
  stderr, or model output.
- M8 binds the host request to the SHA-256 of the exact UTF-8 artifact bytes it
  reads. A mismatch marks the run `SNAPSHOT_STALE` before any host thread starts.
- An M8 host-review lease spans both host turns and all related ledger writes;
  another active owner must receive a lease conflict before it can call a model.

## Retention Policy

- Complete, caller-attested-redacted terminal artifacts remain in the
  plugin-owned SQLite ledger for 30 days.
- After 30 days, a confirmed retention sweep purges the entire plugin-owned
  run and its cascading ledger records; there is no intermediate archive.
- Mark a non-terminal run `STALE` after 14 days without activity; it follows
  the same lifecycle.
- `retention-preview` is read-only. `retention-sweep --confirm-plan-hash` may
  apply only the exact previewed plan; it has no daemon or automatic schedule.
- `nlp-refinement-doe-prune` is an explicit user command for removing expired,
  well-formed M8 DOE failure receipts. It is the only guaranteed cleanup trigger
  when no subsequent DOE is run.
- Logical purge is not a promise of forensic erasure from SQLite free pages,
  backups, WAL files, or external copies.

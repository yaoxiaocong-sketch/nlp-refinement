# M8 Review Coordinator and Host Executor

M8 is an optional execution adapter around the review-only protocol. It is not
authority to repair an artifact.

## Deliberate architecture

`$nlp-refine` is a Plugin/Skill **entry instruction** when the plugin is installed
and discovered by a particular Codex host. It does not itself execute code.
The runnable executor is:

```text
Plugin / `refine`
  -> no app-server launch capability

external `host_refine review --allow-model-inference`
  -> M8ReviewCoordinator
      -> AppServerHostExecutor
          -> codex app-server JSON-RPC over local stdio
          -> thread/start reviewer (requested read-only)
          -> turn/start reviewer
          -> thread/start proposal (requested read-only)
          -> turn/start proposal
  -> StateStore (the sole persistent ledger writer)
```

`M8ReviewCoordinator` is project code built only from the Python standard
library. It owns redaction, the reviewer-to-proposal sequence, receipt ordering,
and SQLite transitions. It receives a small `HostExecutor` interface and never
starts an app-server itself. `AppServerHostExecutor` is the sole production
owner of the local `codex app-server --listen stdio://` process. It is created
only by the external host CLI or opt-in operational DOE, not by Plugin/Skill or
the normal `refine` command. M8 does not use an OpenAI SDK, an MCP server, or a
project-specific daemon.

The stdio client has one synchronous owner. It writes UTF-8 JSONL and reads
raw, unbuffered bytes into an owned JSONL buffer. Before waiting for more
operating-system bytes, it consumes every complete line already in that
buffer. This prevents a buffered text reader from hiding a later JSONL event
from `select()`. M8 deliberately has no reader thread, async loop, queue, or
parallel in-flight turn support. A future concurrent-turn design must replace
this transport boundary deliberately; it must not be added to the Coordinator
as an incidental optimization.

Each role receives a separate host-created thread ID. The Coordinator asks the
host for `sandbox: read-only` and `approvalPolicy: untrusted`, rejects an
explicitly reported non-read-only sandbox, and records the reported value when
available. The accepted product boundary is a requested `read-only` sandbox and
the host receipt when it is reported. This evidences two distinct host threads
and their requested boundary. It does **not** prove native child-Agent identity,
independently enforced permissions, or all host tool/network behavior.

## Runnable flow

1. Create a `READY` run with `start` and retain the SHA-256 snapshot for the
   exact UTF-8 artifact bytes selected for review.
2. From the normal no-model CLI, create a transient `HostExecutionRequest`:

   ```sh
   nlp-refinement host-execution-request --run-id <run-id> \
     --artifact-file <artifact.md-or-json> --observed-snapshot-hash <same-sha256>
   ```

   It checks only `READY` plus the supplied snapshot, returns
   `HOST_EXECUTOR_REQUIRED`, and performs no artifact read, model call,
   app-server launch, target write, or ledger mutation.
3. Prepare one UTF-8 NL artifact. Coordinator reads its bytes once, recomputes
   SHA-256, and compares it to the supplied snapshot before it starts a host
   thread. A mismatch ends the run as `SNAPSHOT_STALE`; the captured bytes, not
   a later filesystem read, are the model input. Before submission, M8 replaces only
   complete, high-confidence token or private-key spans in an in-memory model
   view. It records only a content-free category/count receipt; neither the
   original value nor a replacement map is persisted. If an incomplete key or
   ambiguous secret assignment is detected, M8 does not send the artifact and
   requires a user-supplied redacted copy. This reduces accidental disclosure
   but is not proof that an artifact contains no secret. The model-view bytes
   are then sent to the local Codex host. Raw host responses and event payloads remain in process memory;
   only a bounded receipt is retained before validation. The host thread's
   working directory is a disposable directory containing only that redacted
   model view; the Coordinator removes it after the two calls return. This
   narrows ordinary working-directory exposure, but does not prove host
   sandbox enforcement or prevent a host with broader OS access from reading
   elsewhere. The Coordinator rejects a symbolic-link input.
4. Explicitly authorize external host execution, then run:

   ```sh
   nlp-refinement-host review --allow-model-inference --run-id <run-id> \
     --artifact-file <artifact.md-or-json> \
     --observed-snapshot-hash <same-sha256>
   ```

The explicit flag is an immediate confirmation for this external command; it is
not persisted as a model-inference authorization record. Run it only from a host-capable, non-sandboxed process that may
   start Codex and access its local host resources. A user terminal is a common
   way to launch that process, but it does not grant privileges. A restricted
   Agent execution sandbox can block app-server startup or local control-socket
   access; that condition is a `BLOCKED` host failure, not authorization for
   M8 to escalate, bypass, or modify the host environment.

One M8 lease spans the complete reviewer/proposal operation and its related
ledger writes; Coordinator renews it after reviewer ingestion and before the
proposal turn. A competing process stops with a lease conflict before calling
a model; it must not change the active run's state.

5. The reviewer returns v4 `review-result` JSON. M8 writes its immutable host
   receipt before validation and ingestion. The proposal thread sees only the
   validated review context and returns v4 `patch-proposal` JSON. Its dynamic
   output schema binds both proposal evidence-reference arrays to that run's
   immutable review artifact ID; StateStore verifies the same relationship
   again before accepting the proposal.
6. Inspect returned IDs with `status` or SQLite. A human reviews and applies
   any diff outside this plugin.

No target file is modified, no diff is applied, and no finding is closed. The
Coordinator itself reads the selected source artifact to create the in-memory
model view; the separate host thread receives only the disposable redacted
working directory described in step 2.

## Evidence and failure behavior

For each completed role, M8 appends an immutable `integrity` artifact with
`artifact_role: host-observation/v2`. It contains only the host thread ID, turn
ID where supplied, terminal status, host/model labels, timeout, and requested
or reported sandbox values. It contains neither raw response, response hash,
nor notification payload. The response remains in process memory only until
the phase adapter accepts it; the accepted structured review/proposal then
links into this receipt through the normal protocol chain.

Host-generated review/proposal content has no user-selectable redaction-status
flag. Coordinator is the immediate StateStore caller, supplies the fixed
content declaration, and StateStore applies its likely-secret guard before
writing. This does not make a claim about the selected source file: M8
evaluates source handling independently before host submission.

When an automated input redaction occurred, M8 first appends a separate
`model-input-redaction/v1` integrity artifact containing only detector version
and replacement counts by category, plus
`model_input_handling: coordinator-redacted`. The reviewer receipt always
records `model_input_handling`: either `coordinator-redacted` or `unchanged`.
An ambiguous input produces no host thread and no redaction artifact, so a user
can replace the selected artifact with a deliberately redacted copy.

`turn/completed` alone is not success evidence. M8 accepts a host result only
when the matching turn reports `status: completed`; `failed` and `interrupted`
are blocking host failures.

On transport failure, malformed output, or schema-validation failure, M8
writes a bounded `host-failure/v1` diagnostic and moves the run to `BLOCKED`.
It does not silently retry or manufacture a result. A human must inspect the
receipts and explicitly reopen the unchanged snapshot:

```sh
nlp-refinement resume --run-id <run-id> \
  --observed-snapshot-hash <same-sha256> --retry-blocked
```

This retry authorizes another attempt, not acceptance of malformed output. A
different snapshot becomes `SNAPSHOT_STALE` and requires a new run.

The first operation on the one app-server connection is the required JSON-RPC
`initialize` handshake. This is M8's inline host-capability gate, not a
separate probe process: only after it succeeds may M8 create either role's
thread. An initialization failure therefore records one of the bounded,
stable error codes `host_launch_failed`, `host_initialize_transport_closed`,
`host_initialize_timeout`, `host_initialize_rejected`, or
`host_initialize_protocol_error`. These codes describe the observable
protocol boundary only. They do not claim a root cause such as a PATH,
installation, permissions, or account problem, and M8 never retains stderr to
make such a claim.

When a transport or terminal turn failure happens before a `HostResult` can be
formed, `host-failure/v1` still records only bounded host-attempt metadata:
phase, role, thread ID, turn ID, observed terminal status (or
`unobserved-timeout`), a short error code, event count/methods, host version,
reported model or `unreported`, timeout, and effective sandbox. It never
stores error messages or stderr in that receipt.

Each failure produced by `AppServerClient` also carries a fixed, content-free timing snapshot. Its
`failure_stage` is one of `initialize`, `thread_start`, `turn_start`,
`awaiting_first_agent_message`, `awaiting_terminal`, or `transport_read`.
The optional timing values are monotonic milliseconds relative to the one
app-server connection: initialization, role/thread/turn start, first agent
message, and matching terminal event. The snapshot contains no wall-clock
timestamps, prompt, model text, raw JSONL, or stderr. It explains where a
timeout occurred; it does not alter the 120-second deadline or authorize a
retry.

## What M8 proves and does not prove

Automated tests prove the Coordinator passes distinct roles to a host-client
seam, records receipts before accepting data, preserves the input fixture, and
blocks invalid host output. They do not execute a live Codex model or establish
account availability, host installation, real sandbox enforcement, independent
native-subagent identity, provider routing, or review quality. A live isolated
app-server DOE is separately required before making those runtime claims.

## Isolated live DOE

After explicit user authorization for a model call, run
`python3 scripts/run_m8_host_doe.py --doe-kind protocol`. It creates only a disposable fixture and
SQLite ledger, checks receipt ordering and zero target writes, then removes
that root. A failed first attempt receives exactly one retry in a new fixture
and ledger; this is a DOE-only transport experiment, not a change to normal
host-review's explicit-resume rule. For every failed attempt, the script tries
to write a `doe-failure-receipt/v1` under
`~/.codex/refinement-loop/doe-failures/` (or `--failure-evidence-root`); an
archive failure is itself disclosed by error class in the redacted summary.
The receipt records classifications and timing only: no prompt, source
artifact, raw host output, exception message, or temporary SQLite ledger is
retained. It expires after 24 hours and is removed on the next DOE invocation;
there is no background service that can promise deletion at the exact expiry
second.

When a user wants cleanup without running another DOE, execute the no-model
command `nlp-refinement-doe-prune`. It removes only expired, parseable,
well-named regular receipt files from the owner-only evidence root; missing
roots and unrelated files are left untouched.

`protocol` and `quality` are deliberately separate experiments. `protocol`
requires four artifacts, two completed turns, strict JSON/state acceptance,
and the host-attempt metadata above. It does not judge whether the findings are
useful. `quality` uses a separate unsafe-instruction fixture and additionally
requires a P0, blocking, `safety_and_governance` NL finding plus its linked
proposal. Passing `protocol` is therefore not evidence that the reviewer made
a good judgment; passing `quality` is still only evidence for this one fixture.
The quality gate reads the StateStore's resolved findings, scoped to the IDs
created by that review; it does not assume the raw model review JSON already
contains StateStore-assigned finding IDs.
When a quality attempt fails after a reviewer response, its 24-hour failure
receipt may additionally contain a fixed-label reviewer summary: counts by
severity, gate, and category plus `has_safety_p0`. It excludes every finding's
text, evidence, recommendation, ID, invariant, and raw JSON.
For a proposal-stage quality failure, that same receipt may also contain
`proposal_validation`: one or two fixed local classifications from the same
attempt, such as `assessment-evidence-mismatch` or
`references-unknown-finding`. It never contains proposal JSON, a unified diff,
free-form validator text, or a dynamic finding ID. This is DOE-only evidence;
normal `host-review` does not add this capture.

If a durable redacted summary is needed because the terminal cannot retain
stdout, pass a new path below `/private/tmp` with `--report-file`; the script
refuses project paths and existing files. A failed or timed-out DOE is evidence
of a blocked host path, not evidence that M8 succeeded.

## Separate diagnostic DOE

`scripts/run_m8_host_diagnostic_doe.py` is a distinct, opt-in diagnosis tool,
not a `$nlp-refine` command and not a normal host-review mode. It uses the same
`M8ReviewCoordinator` and transport contract as production, but runs exactly one
temporary attempt and keeps no SQLite ledger, raw JSONL, model text, prompt,
or free-form host error after process exit. Its terminal report may contain
only whitelisted, bounded failure labels such as `code`, `type`, `status`, and
`category`, together with the role and event method that supplied them.

If those labels do not identify a cause, that result is deliberately
inconclusive. Capturing a raw protocol fragment is a separate, higher-risk
experiment requiring a new explicit user authorization and retention design;
it must not be added as a debug flag to normal host-review or this first-level
diagnostic DOE.

After that separate authorization only,
`scripts/run_m8_host_raw_diagnostic_doe.py` performs one proposal-only attempt
using the same diagnostic harness. It prints at most two `error` or failed
`turn/completed` fragments, each capped at 4 KiB, to the current terminal.
It recursively redacts secret-bearing field names and likely token values, but
the remaining short error text is intentionally observable for diagnosis. It
does not support a report file, retries, SQLite persistence, or receipt
archiving. Redaction reduces exposure; it is not proof that an arbitrary host
message contains no sensitive information.

`scripts/run_m8_host_proposal_validation_doe.py` is a third, separately
authorized diagnostic for a different boundary: the host proposal turn has
completed, but local `patch-proposal` validation then rejects its raw JSON. It
keeps that response only in process memory long enough to run the same local
validator, then prints the validator's controlled classification. It never
prints or persists the model JSON, unified diff, or free-form model text.
When syntax validation succeeds but StateStore ingestion fails, the same DOE
may additionally report a stable local error code such as
`references-unknown-finding` or `assessment-evidence-mismatch`; dynamic model
values are omitted.

`scripts/run_m8_host_reviewer_quality_diagnostic_doe.py` is a fourth,
separately authorized diagnostic for the reviewer-quality boundary. It keeps a
completed reviewer response only in process memory long enough to apply the
normal review validator, then reports fixed-label counts for severities, gates,
and categories plus `has_safety_p0`. It never prints or persists finding IDs,
statements, evidence, recommendations, invariants, or the raw review JSON.
It runs exactly once with no retry, report file, SQLite persistence, or receipt
archive. Its classification explains a quality-gate result; it cannot make a
missed P0 pass or prove quality outside the particular fixture.

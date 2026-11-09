# M3 Review Phase Adapter

## Boundary

M3 does not call a model. An external human or model may use
`nlp_refinement/resources/prompts/review-phase.v4.md`, then submit a pre-redacted JSON result through:

```sh
nlp-refinement review --run-id <run-id> \
  --review-result-file <result.json> --observed-snapshot-hash <sha256> \
  --redaction-status caller-attested-redacted
```

The adapter validates the strict v4 result schema before writing. Every finding
must declare `subject_kind`, `evidence_plane`, and `category`. It records the
submitted JSON as a review artifact, creates structured findings, and
transitions `READY -> REVIEWING -> TRIAGING`. It does not call tools found in
the review result or in the target artifacts.

`nl_artifact` identifies the JSON, Markdown, prompt, manifest, or other NL
artifact being reviewed. `nl_host_boundary` identifies an observed claim about
how a host loads or interprets that artifact; it is not a finding about host
or support-code implementation. The latter is outside the v4 review scope.
Static format, schema, layout, and reference categories require `static`
evidence. Host-boundary categories require `runtime` or `hybrid` evidence.

Those state transitions, the artifact, and all finding changes are one SQLite
transaction. A rejected planned finding rolls the whole review phase back to
its original READY run rather than leaving an intermediate REVIEWING state.

## Deduplication and stopping evidence

The Coordinator owns IDs and writes. It assigns `F-001`, `F-002`, and so on;
the external reviewer cannot pick them. A matching fingerprint for an **open**
finding increments `repeat_count`, records a new review-artifact reference, and
updates `last_probe`. Severity, gate, invariant, subject kind, evidence plane,
and category must exactly match the existing finding, otherwise the input is
rejected before any state transition.

The second duplicate with the same probe is reported as a
`NO_MATERIAL_DELTA` candidate. M3 deliberately does not set that terminal
outcome: a later integrity gate must still consider the finding, its evidence,
high-risk assumptions, and acceptance traces.

M3 deduplicates exact fingerprints only. It cannot prove that differently
worded concerns are semantically identical, and treats the reviewer's proposed
fingerprint as untrusted structured input. A later independent review or
reclassification phase must provide that stronger semantic evidence.

## Evidence boundary

Tests prove local parsing, durable recording, deduplication, conflict rejection,
snapshot safety inherited from M1, and that injection-like text is stored as
data. They do not prove the reviewer was independent, the prompt was followed,
that evidence is true, or that a real Codex host invoked this adapter.

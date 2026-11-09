# M6 Finding Taxonomy

## Scope

V4 reviews natural-language-programming artifacts: JSON, Markdown, prompts,
manifests, rules, Skills, Agents, Commands, and their fixtures. It does not
review Python, JavaScript, Swift, Shell, database, operating-system, or host
implementation defects as NL findings.

Every finding records three independent labels:

| Field | Meaning | Values |
|---|---|---|
| `subject_kind` | What is being assessed | `nl_artifact`, `nl_host_boundary` |
| `evidence_plane` | What kind of evidence supports it | `static`, `runtime`, `hybrid` |
| `category` | The specific concern family | Listed below |

`nl_host_boundary` does not turn host code into review scope. It records only
whether a host was observed loading or interpreting an NL artifact as the
artifact claims. A host or support-code defect must be reported outside this
finding protocol.

## Categories and valid evidence

| Subject | Category | Allowed evidence |
|---|---|---|
| `nl_artifact` | `format_and_encoding`, `schema_contract`, `layout_and_discovery`, `reference_integrity` | `static` only |
| `nl_artifact` | `semantic_workflow`, `safety_and_governance`, `verification_design` | `static` or `hybrid` |
| `nl_host_boundary` | `load_and_discovery`, `instruction_adherence`, `state_behavior`, `outcome_verification`, `safety_behavior` | `runtime` or `hybrid` |

Examples: invalid JSON is `nl_artifact` / `static` /
`format_and_encoding`; a Skill reference that does not resolve is
`nl_artifact` / `static` / `reference_integrity`; an observed host that ignores
a declared stop rule is `nl_host_boundary` / `runtime` /
`instruction_adherence`.

## Integrity rules

The Coordinator validates the label combination before leasing or writing a
review result. A duplicate fingerprint must retain the original severity,
gate, invariant, subject kind, evidence plane, and category. A changed label
is rejected rather than treated as a new, lower-priority concern. This keeps
the taxonomy from being used for priority laundering.

V4 has no earlier-version compatibility reader. If a local ledger contains an earlier run,
the StateStore stops with an explicit legacy-ledger error; it never overwrites,
migrates, or silently reinterprets that run.

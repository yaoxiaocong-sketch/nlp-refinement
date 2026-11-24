# NLP-refinement

Display name: **NLP-refinement**
Technical identifier: `nlp-refinement-cli`

NLP-refinement is a local-first Codex Plugin for a human-only review and
proposal workflow over natural-language-programming artifacts. Its supported
flow is:

```text
TUI root -> reviewer -> verified findings -> proposer -> independent assessor
         -> human-only v6 proposal
```

The TUI root creates three distinct generic child contexts. The local
`nlp-refinement` CLI scans candidate artifacts, creates and queries the SQLite
ledger, checks snapshots and schemas, builds controlled handoffs, and records
the final proposal. It does not write target artifacts, apply a proposal,
execute a diff, or start a controller, daemon, MCP bridge, or app-server.
Every persisted proposal carries `no_target_write_attestation: true`; this is
an auditable protocol boundary rather than an OS-sandbox claim.

## Entry points

Use the namespaced Plugin Skill in any target project:

```text
$nlp-refinement-cli:nlp-review-propose start
$nlp-refinement-cli:nlp-review-propose resume
```

`start` automatically creates a Batch for every `AUTO` artifact admitted by
the versioned deterministic NL policy. It does not show a numbered candidate
table or ask the user to choose files. Recognized policy artifacts that exceed
the 1 MB default limit or are not strict UTF-8 are reported as `RESTRICTED`:
they are recorded for maintainer review but are not decoded, truncated,
transcoded, or reviewed. `resume` recovers verified facts from the ledger; it
never relies on an old child transcript or scratch file.

Use `$nlp-refinement-cli:nlp-review-propose maintenance` to inspect an OPEN
capability-review event and record a maintainer decision. This is not a path
for force-including a restricted file.

## Local CLI setup

The Plugin distributes Skills and a pinned setup flow. The separately installed
Python distribution supplies one public command:

```sh
nlp-refinement --help
```

On first use, `$nlp-refinement-cli:nlp-refine-setup` may show a hash-pinned
installation plan. It only downloads and installs after the user confirms that
exact plan. A matching installed CLI must expose `runtime-identity` for the
TUI-native interface; source checkout and guessed cache paths are not fallback
runtime sources.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 scripts/validate_m0_contract.py
```

The acceptance record is in
[TUI native review/propose acceptance](docs/TUI_NATIVE_REVIEW_PROPOSE_ACCEPTANCE.md).
It distinguishes verified protocol behavior from unverified host-enforced role
binding, child cwd, and sandbox isolation.

## Compatibility and history

The current source accepts only the TUI-only v3 release-manifest schema and
publishes only `nlp-refinement`. Earlier tags, GitHub Releases, and PyPI
distributions remain immutable external history, but are not supported runtime
inputs for this source tree. See the
[current-only decommission record](docs/TUI_ONLY_DECOMMISSION_PLAN.md).

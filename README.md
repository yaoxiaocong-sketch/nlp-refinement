# NLP-refinement

Display name: **NLP-refinement**
Technical identifier: `nlp-refinement-cli`

This repository contains M0, the protocol and fixture baseline; M1, a local
SQLite ledger; M2, a review-only command adapter; and M3, structured external
review-result ingestion for a local-first
refinement loop over Skills, plugins, prompts, rules, agents, and their
fixtures. M4 adds only a temporary-fixture repair feasibility DOE. M5 records
human-only patch proposals. M6 makes the v5 NL finding taxonomy mandatory; M7
adds finding-assessment and proposal-score contracts; M8 adds an optional
standard-library Coordinator that asks the local Codex app-server for separate
reviewer and proposal threads. No milestone modifies target artifacts.

## CLI installation

The Plugin/Skill is an instruction and discovery surface. The local executable
is a separately installed Python distribution; Plugin installation never
installs it implicitly. For a published, configured release, the first
`$nlp-refine` setup flow may present a pinned CLI installation plan and, only
after explicit user confirmation, download, verify, and install that exact
wheel. The setup wrapper first checks all three CLI commands through
`--version`; it treats an older command as incompatible rather than as a valid
installation. A release configuration is valid only when its matching public
GitHub Release manifest and wheel assets exist.

Manual/offline installation remains supported:

```sh
python3 -m pip install --user /path/to/nlp_refinement_cli-0.9.1-py3-none-any.whl
```

That distribution provides two deliberately separate execution commands plus a
no-model DOE receipt-maintenance command:

```sh
nlp-refinement --help
nlp-refinement-host --help
nlp-refinement-doe-prune --help
```

`nlp-refinement` is the normal no-model command. Only the separately invoked
`nlp-refinement-host` command may request the local Codex app-server after an
explicit `--allow-model-inference` confirmation. Plugin retention covers only
its SQLite ledger; Codex host session history follows Codex configuration. If either command is absent,
stop and install the matching CLI; do not fall back to a source checkout,
`PYTHONPATH`, or a guessed plugin-cache path.

See [first-use runtime bootstrap](docs/RUNTIME_BOOTSTRAP.md) for the plan,
confirmation, hash-check, failure, and platform boundaries.
See [PyPI Trusted Publishing](docs/PYPI_TRUSTED_PUBLISHING.md) for the
manual-only public-package release path; adding that workflow does not publish
a package. The separate Linux verification workflow has no PyPI authority.

`nlp-refinement-doe-prune` is also a no-model command. It removes only expired,
well-formed redacted M8 DOE failure receipts from its owner-only evidence root;
it does not create a missing root or delete unknown files.

Run the current validation suite with:

```sh
python3 -m unittest discover -s tests -v
```

See the [M0 protocol](docs/M0_PROTOCOL.md) for the selected decisions and
state-machine contract, and the [M1 StateStore](docs/M1_STATESTORE.md) for the
implemented durable-local ledger boundary. See [M2 command adapter](docs/M2_COMMAND.md)
for the runnable start/status/resume/retention boundary and [M3 review adapter](docs/M3_REVIEW_ADAPTER.md)
for external review-result ingestion.

See [M6 finding taxonomy](docs/M6_FINDING_TAXONOMY.md) for the static,
runtime-boundary, and evidence classifications accepted by review ingestion.
See [M7 severity and score](docs/M7_FINDING_SEVERITY_AND_PROPOSAL_SCORE.md) for
the independent finding assessment fields and proposal hard gates.
See [M8 Host Coordinator](docs/M8_HOST_COORDINATOR.md) for the optional local
app-server JSON-RPC executor, its receipts, and runtime-evidence limits.

The [M4 repair feasibility DOE](docs/M4_REPAIR_FEASIBILITY_DOE.md) records
observed local primitives and explicit non-proofs. It is not a repair feature.
See [M5 human-only patch proposals](docs/M5_HUMAN_PATCH_PROPOSALS.md) for the
manual-application boundary.

## Contributing

External contributors may propose changes by forking the repository and
opening a Pull Request; they do not receive direct repository write or PyPI
publication access. See [CONTRIBUTING.md](CONTRIBUTING.md) for the review and
licence boundary. The root [LICENSE](LICENSE) and [NOTICE](NOTICE) declare the
Apache-2.0 licence and copyright-holder notice for public distribution.

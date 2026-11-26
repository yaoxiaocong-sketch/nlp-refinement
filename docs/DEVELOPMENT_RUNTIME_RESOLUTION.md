# Development runtime resolution

This document defines a developer-only way to bind a TUI acceptance run to one
installed feature CLI without relying on `PATH`. It is not a public installer,
a replacement for the release bootstrap, or an OS sandbox.

## Why it exists

An executable name and a distribution version are not sufficient identity. A
login shell can reorder `PATH`, and two installations can report the same
version while exposing different command sets. A native TUI workflow must stop
before a Batch or model call when it cannot prove that its selected CLI has the
TUI-native interface.

## Developer descriptor

The developer creates a JSON file outside the target project. The descriptor
is a developer-control artifact and must not be committed to a reviewed target
project. The developer invokes it explicitly for the one acceptance run or
recovery:

```text
$nlp-review-propose start --development-runtime-descriptor /absolute/path/to/development-runtime.json
$nlp-review-propose resume --development-runtime-descriptor /absolute/path/to/development-runtime.json
```

The normal user entries remain `$nlp-review-propose start` and
`$nlp-review-propose resume`. The Skill must not
discover a descriptor from `PATH`, the project, an environment variable, or a
plugin cache path: those can be changed by login shells or untrusted project
artifacts without an explicit developer decision.

```json
{
  "schema_version": "nlp-refinement-development-runtime/v1",
  "executable": "/absolute/path/to/isolated-venv/bin/nlp-refinement",
  "state_root": "/absolute/path/to/isolated-ledger",
  "wheel_sha256": "<64 lowercase hexadecimal characters>",
  "expected_identity": {
    "distribution_name": "nlp-refinement-cli",
    "distribution_version": "<feature-build-version>",
    "interface_version": "nlp-refinement-tui-native/v1",
    "commands": [
      "runtime-identity",
      "scan",
      "policy-observe",
      "capability-review",
      "batch-create",
      "resume",
      "tui-resume-locators",
      "tui-resume-candidates",
      "tui-workflow-disposition",
      "tui-review-handoff",
      "tui-proposal-handoff",
      "tui-proposal-draft-handoff",
      "tui-compose-assessed-proposal",
      "tui-create-transient-root",
      "tui-record-review",
      "tui-record-generated-dispatch-receipt",
      "tui-record-child-dispatch-failure",
      "tui-record-child-output-failure",
      "tui-child-dispatch-receipts",
      "tui-cleanup-transient-root",
      "tui-child-contract",
      "tui-inline-handoff",
      "patch-proposal"
    ]
  }
}
```

The descriptor must name exactly the CLI console script installed in the
isolated environment. Before the Skill runs a workflow command, it invokes:

```sh
/absolute/path/to/isolated-venv/bin/nlp-refinement runtime-resolve \
  --descriptor /absolute/path/to/development-runtime.json
```

`RESOLVED` means only that the invoked absolute executable matches the
descriptor and advertises the expected TUI-native interface. It does not prove
that the wheel was independently authenticated at runtime, that a child has an
OS sandbox, or that native-child orchestration works. The wheel hash is
traceability data from the explicit developer build/install receipt.

`state_root` is also absolute and is returned by the resolver. The Skill passes
it explicitly as `--state-root` to every later scan, Batch, adapter, and
recovery invocation. It must not rely on `NLP_REFINEMENT_STATE_ROOT`: a TUI
tool shell may reset environment variables before launching a CLI.

## Failure behavior

The resolver stops, without creating StateStore data or calling a model, for:

- descriptor schema or field mismatch;
- a relative, missing, or non-file executable path;
- an executable path different from the one that was invoked;
- identity/version/interface disagreement;
- absent TUI-native commands.

There is no automatic fallback to `PATH`, source checkout execution,
`PYTHONPATH`, plugin cache locations, or the global CLI.

## Relation to release installation

This is a development acceptance mechanism. The public workflow remains the
hash-pinned release bootstrap in [RUNTIME_BOOTSTRAP.md](RUNTIME_BOOTSTRAP.md):
it obtains a publisher-controlled wheel only after user confirmation, then
checks the installed release. A release acceptance must still exercise that
path separately.

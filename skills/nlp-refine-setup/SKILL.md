---
name: nlp-refine-setup
description: Set up the separately distributed NLP-refinement CLI after the user explicitly approves the pinned installation plan.
---

# NLP-refinement local-runtime setup

This onboarding Skill configures the local Python CLI required by
`$nlp-refine`. It is not a normal review command and must never start the
Codex app-server or inspect a target artifact.

## Required consent boundary

First determine whether all three commands are available on `PATH`:

- `nlp-refinement`
- `nlp-refinement-host`
- `nlp-refinement-doe-prune`

If all are available at the Plugin-compatible version, report setup complete.
Do not download, install, upgrade, uninstall, or change `PATH`.

If any command is missing or incompatible, present a **plan only**. The plan
must state the fixed CLI version, package name, release-manifest URL, manifest
SHA-256, wheel SHA-256, Python requirement, and that installation uses the
current Python interpreter's user site. Do not execute an installer until the
user explicitly confirms this exact plan in the current conversation.

After an explicit confirmation, run only the packaged
`scripts/nlp_refinement_runtime_bootstrap.py` from the absolute Plugin root
provided by the host. Do not infer, search for, or guess a Plugin cache path.
Pass the release-manifest URL, its SHA-256, and the root `plugin.json` as
explicit arguments. Use `--apply` only after the plan has been shown and
confirmed.

If the host does not provide an absolute Plugin root or a pinned release
manifest configuration, stop with `BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE`.
Do not compose a shell download command, use a model-supplied URL, or bypass
the bootstrap's HTTPS and SHA-256 checks.

The bootstrap result is authoritative for this setup attempt:

- `PLAN_READY`: show the plan and wait for confirmation.
- `INSTALLED`: ask the user to start a fresh Codex session, then use
  `$nlp-refine` normally.
- `BLOCKED`: show only its code, completed stages, and next action. Do not
  retry automatically or uninstall a possibly pre-existing CLI.

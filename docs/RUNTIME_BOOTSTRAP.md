# First-use local-runtime bootstrap

`$nlp-refine` is the discovery and workflow entry point. The separately
distributed Python CLI is the executable component. The Plugin must not
silently install Python code merely because a user invoked a Skill.

For a published release, the Plugin contains an onboarding Skill, a
standard-library setup wrapper, and `runtime-bootstrap-config.json`. The host
must provide the absolute Plugin root. The wrapper reads its root-owned config,
checks that all three CLI commands report the matching version, and otherwise
uses the config's HTTPS URL and SHA-256 to fetch the expected release manifest.
That release manifest then pins the wheel URL and wheel SHA-256.

The first setup flow is:

1. Check whether the expected CLI commands are already available and report
   the matching version through `--version`.
2. Fetch and hash-check only the pinned release manifest and produce
   `PLAN_READY`; this does not install anything.
3. Show the user the exact version and hashes.
4. Only after explicit confirmation, fetch and hash-check the wheel, its exact
   byte size, and its embedded package metadata, then run
   `python -m pip install --user --no-deps --no-index <verified-wheel>`.
5. Check each installed command on `PATH`.

An installer failure reports a fixed code and the stages that completed. It
does not perform an automatic uninstall because that could remove a CLI that
pre-dated this setup attempt. A `CLI_PATH_MISMATCH` result means the package may
have installed, but the user must update `PATH` or start a new terminal or
Codex session before the Plugin can rely on it.

## Release and host boundaries

Each released Plugin carries a version-specific configuration that pins a
publisher-controlled HTTPS release manifest. The configuration and manifest
must be committed, tested together, and released as versioned GitHub Release
assets with the matching wheel. The bootstrap accepts the asset only when its
pinned SHA-256 matches. If the host cannot provide an absolute Plugin
root, or the configuration is missing or invalid, the onboarding Skill must
report `BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE`. If the configured public asset
is unavailable, the bootstrap must report `RELEASE_MANIFEST_DOWNLOAD_FAILED`.
It must not invent a source.

This is desktop/CLI-local behavior only. It is not a claim that web, mobile,
or IDE Plugin surfaces can execute a local Python bootstrap. It also does not
use a lifecycle hook: public Plugin distribution and hook trust have different
host constraints.

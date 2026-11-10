# First-use local-runtime bootstrap

`$nlp-refine` is the discovery and workflow entry point. The separately
distributed Python CLI is the executable component. The Plugin must not
silently install Python code merely because a user invoked a Skill.

For a published release, the Plugin contains an onboarding Skill and a
standard-library bootstrap script. The host must provide the absolute Plugin
root and a release manifest configuration pinned to both a HTTPS URL and the
SHA-256 of the expected manifest. The release manifest then pins the wheel URL
and wheel SHA-256.

The first setup flow is:

1. Check whether the expected CLI commands are already available.
2. Fetch and hash-check only the pinned release manifest and produce
   `PLAN_READY`; this does not install anything.
3. Show the user the exact version and hashes.
4. Only after explicit confirmation, fetch and hash-check the wheel, its exact
   byte size, and its embedded package metadata, then run
   `python -m pip install --user --no-deps --no-index <verified-wheel>`.
5. Check each installed command on `PATH`.

An installer failure reports a fixed code and the stages that completed. It
does not perform an automatic uninstall because that could remove a CLI that
pre-dated this setup attempt. A `CLI_NOT_ON_PATH` result means the package may
have installed, but the user must update `PATH` or start a new terminal or
Codex session before the Plugin can rely on it.

## Release and host boundaries

The source repository deliberately carries no production release URL or pinned
hash. Adding one is a publishing decision and requires a release artifact,
publisher-controlled HTTPS location, and an isolated end-to-end bootstrap DOE.
Until then, the onboarding Skill must report
`BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE`; it must not invent a source.

This is desktop/CLI-local behavior only. It is not a claim that web, mobile,
or IDE Plugin surfaces can execute a local Python bootstrap. It also does not
use a lifecycle hook: public Plugin distribution and hook trust have different
host constraints.

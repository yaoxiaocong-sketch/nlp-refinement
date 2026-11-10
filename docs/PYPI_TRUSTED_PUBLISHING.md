# PyPI Trusted Publishing

This project is configured to publish `nlp-refinement-cli` from the GitHub
repository `yaoxiaocong-sketch/nlp-refinement` through PyPI Trusted Publishing
once the pending publisher has been added in PyPI. The workflow is deliberately
manual: adding it does not publish a package.

## One-time PyPI configuration

Before the first manual workflow run, the PyPI account owner (`yaoxiaocong`)
must add a GitHub Actions pending Trusted Publisher in PyPI account settings.
Use exactly these values:

| PyPI field | Value |
| --- | --- |
| PyPI project name | `nlp-refinement-cli` |
| Owner | `yaoxiaocong-sketch` |
| Repository | `nlp-refinement` |
| Workflow filename | `pypi-publish.yml` |
| Environment | leave empty |

The empty environment is intentional: the workflow declares no GitHub
environment. Changing either side later requires changing the matching value
on the other side before a publish can succeed.

`.github/workflows/verify.yml` separately runs the source tests on Linux for
pushes and pull requests. It has no PyPI authority and never builds or uploads
a distribution. A failed verification must be fixed before manually running
the publishing workflow.

## What the workflow does

`.github/workflows/pypi-publish.yml` has only a `workflow_dispatch` trigger.
It cannot run because a branch is pushed, a pull request is opened, or a GitHub
release is created. A maintainer must deliberately choose **Run workflow** in
the GitHub Actions page.

The `verify-release-asset` job has only `contents: read`. It checks out the
selected revision, runs the local test and contract suites, then downloads the
wheel already attached to the matching GitHub Release. Before staging it for
the publish job, `fetch_verified_runtime_release.py` verifies the committed
Plugin configuration, the release-manifest hash, wheel byte size, wheel SHA-256
and embedded package metadata. It never builds a replacement wheel.

The publish job has only `id-token: write`. It obtains a short-lived identity
from GitHub Actions and gives it to PyPI through
`pypa/gh-action-pypi-publish@release/v1`. It contains no PyPI password, API
token, or repository secret.

Running the workflow after the pending publisher has been configured will
publish those same verified wheel bytes to PyPI. Do not run it until the
matching GitHub Release has been created and the version, package contents,
and release decision have been reviewed.

## One wheel, two channels

For every new version, the publisher must follow this order:

1. Finish all package-source and documentation changes, then run the local
   source tests.
2. Build exactly one wheel locally, for example
   `python -m build --wheel --no-isolation --outdir dist`.
3. Run `scripts/prepare_runtime_release.py` first without `--apply`, review its
   hash and planned paths, then rerun it with `--apply`. It writes only the
   new versioned release manifest and the mutable Plugin bootstrap config.
   It refuses to replace an existing versioned manifest.
4. Re-run the test and contract suites, commit and tag the resulting source
   records, then create the matching GitHub Release. Upload the exact wheel
   from step 2 and the generated release manifest; do not rebuild.
5. In GitHub Actions, manually run `pypi-publish.yml` for that tag. The
   workflow downloads, re-verifies and publishes the exact GitHub Release
   wheel; it does not run a build command.

The single wheel is publisher-built, rather than independently rebuilt by CI.
CI proves the committed source tests and verifies the released bytes; it does
not claim reproducible builds across toolchains.

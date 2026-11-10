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

`.github/workflows/verify.yml` separately runs the same tests and package
build on Linux for pushes and pull requests. It has no PyPI authority and never
uploads a distribution. A failed verification must be fixed before manually
running the publishing workflow.

## What the workflow does

`.github/workflows/pypi-publish.yml` has only a `workflow_dispatch` trigger.
It cannot run because a branch is pushed, a pull request is opened, or a GitHub
release is created. A maintainer must deliberately choose **Run workflow** in
the GitHub Actions page.

The build job has only `contents: read`. It checks out the selected revision,
runs the local test and contract suites, builds the source distribution and
wheel, then passes only those distribution files to the publish job.

The publish job has only `id-token: write`. It obtains a short-lived identity
from GitHub Actions and gives it to PyPI through
`pypa/gh-action-pypi-publish@release/v1`. It contains no PyPI password, API
token, or repository secret.

Running the workflow after the pending publisher has been configured will
publish the built version to PyPI. Do not run it until the version, package
contents, and release decision have been reviewed.

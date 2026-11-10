from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pypi-publish.yml"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"


class PyPIPublishWorkflowTests(unittest.TestCase):
    def test_publish_workflow_is_manual_and_uses_separate_least_privilege_jobs(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertNotIn("release:", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("fetch_verified_runtime_release.py", workflow)
        self.assertNotIn("python -m build", workflow)
        self.assertNotIn("pip install --upgrade build", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("password:", workflow)

    def test_trusted_publishing_document_binds_the_exact_pending_publisher_fields(self) -> None:
        guide = (ROOT / "docs" / "PYPI_TRUSTED_PUBLISHING.md").read_text(encoding="utf-8")

        self.assertIn("nlp-refinement-cli", guide)
        self.assertIn("yaoxiaocong-sketch", guide)
        self.assertIn("nlp-refinement", guide)
        self.assertIn("pypi-publish.yml", guide)
        self.assertIn("not publish", guide)

    def test_verification_workflow_has_no_pypi_authority(self) -> None:
        workflow = VERIFY_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("push:", workflow)
        self.assertIn("pull_request:", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("gh-action-pypi-publish", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("python -m build", workflow)
        self.assertNotIn("pip install --upgrade build", workflow)
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()

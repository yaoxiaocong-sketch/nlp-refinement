from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContributionPolicyTests(unittest.TestCase):
    def test_contributing_uses_pr_review_and_apache_inbound_license(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        normalized = " ".join(contributing.split())

        self.assertIn("Fork", normalized)
        self.assertIn("Pull Request", normalized)
        self.assertIn("does not grant direct write access", normalized)
        self.assertIn("Apache License 2.0", normalized)
        self.assertIn("right to submit", normalized)

    def test_root_declares_apache_license_and_copyright_holder(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        with (ROOT / "pyproject.toml").open("rb") as handle:
            configuration = tomllib.load(handle)
        project = configuration["project"]

        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("Copyright 2026 yaoxiaocong", notice)
        self.assertEqual(project["license"], {"file": "LICENSE"})
        self.assertEqual(configuration["tool"]["setuptools"]["license-files"], ["LICENSE", "NOTICE"])


if __name__ == "__main__":
    unittest.main()

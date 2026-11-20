from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tomllib
import unittest

from nlp_refinement.refine import build_parser, main
from nlp_refinement.version import CLI_VERSION


ROOT = Path(__file__).resolve().parents[1]
class CliDistributionTests(unittest.TestCase):
    def test_the_only_public_command_reports_the_distribution_version(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            main(["--version"])
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(output.getvalue(), f"{CLI_VERSION}\n")

    def test_distribution_declares_only_the_tui_native_cli(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)
        project = package["project"]

        self.assertEqual(project["name"], "nlp-refinement-cli")
        self.assertEqual(project["version"], CLI_VERSION)
        self.assertEqual(package["build-system"]["requires"], ["setuptools", "wheel"])
        self.assertEqual(project["scripts"], {"nlp-refinement": "nlp_refinement.refine:main"})
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(build_parser().prog, "nlp-refinement")

    def test_distribution_keeps_only_protocol_resources_used_by_the_tui_workflow(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)

        resource_root = ROOT / "nlp_refinement" / "resources"
        self.assertTrue((resource_root / "schemas" / "v5" / "review-result.schema.json").is_file())
        self.assertTrue((resource_root / "schemas" / "inventory" / "v1" / "artifact-inventory.schema.json").is_file())
        self.assertTrue((resource_root / "schemas" / "inventory" / "v2" / "artifact-inventory.schema.json").is_file())
        self.assertEqual(
            package["tool"]["setuptools"]["package-data"]["nlp_refinement.resources"],
            ["schemas/v5/*.json", "schemas/inventory/v1/*.json", "schemas/inventory/v2/*.json", "prompts/*.md"],
        )

    def test_normal_command_does_not_expose_retired_host_execution(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["host-execution-request"])

    def test_source_tree_excludes_retired_m4_m8_execution_surfaces(self) -> None:
        retired_paths = (
            "nlp_refinement/diagnostic_doe_support.py",
            "nlp_refinement/doe_receipt_retention.py",
            "nlp_refinement/host_client.py",
            "nlp_refinement/host_diagnostics.py",
            "nlp_refinement/host_execution_request.py",
            "nlp_refinement/host_executor.py",
            "nlp_refinement/host_refine.py",
            "nlp_refinement/m8_doe_fixtures.py",
            "nlp_refinement/m8_review_coordinator.py",
            "nlp_refinement/repair_feasibility.py",
            "nlp_refinement/resources/prompts/proposal-draft-phase.v1.md",
            "nlp_refinement/resources/prompts/proposal-assessment-phase.v1.md",
        )
        for relative_path in retired_paths:
            self.assertFalse((ROOT / relative_path).exists(), relative_path)


if __name__ == "__main__":
    unittest.main()

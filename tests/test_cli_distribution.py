from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import tomllib
import unittest

from nlp_refinement.refine import build_parser, main
from nlp_refinement.host_refine import build_parser as build_host_parser


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "a" * 64


class CliDistributionTests(unittest.TestCase):
    def test_distribution_declares_separate_normal_and_external_host_commands(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)
        project = package["project"]

        self.assertEqual(project["name"], "nlp-refinement-cli")
        self.assertEqual(package["build-system"]["requires"], ["setuptools", "wheel"])
        self.assertEqual(project["scripts"], {
            "nlp-refinement": "nlp_refinement.refine:main",
            "nlp-refinement-host": "nlp_refinement.host_refine:main",
            "nlp-refinement-doe-prune": "nlp_refinement.doe_receipt_retention:main",
        })
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(build_parser().prog, "nlp-refinement")
        self.assertEqual(build_host_parser().prog, "nlp-refinement-host")

    def test_distribution_keeps_m8_runtime_resources_inside_the_package(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)

        resource_root = ROOT / "nlp_refinement" / "resources"
        self.assertTrue((resource_root / "schemas" / "v4" / "review-result.schema.json").is_file())
        self.assertTrue((resource_root / "prompts" / "review-phase.v4.md").is_file())
        self.assertEqual(
            package["tool"]["setuptools"]["package-data"]["nlp_refinement.resources"],
            ["schemas/v4/*.json", "prompts/*.md"],
        )

    def test_normal_command_advertises_the_external_host_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            brief = root / "brief.md"
            brief.write_text("review this skill", encoding="utf-8")
            state_root = root / "state"
            started = io.StringIO()
            with redirect_stdout(started):
                self.assertEqual(main([
                    "--state-root", str(state_root), "start", "--project-dir", str(project),
                    "--redacted-brief-file", str(brief), "--snapshot-hash", SNAPSHOT,
                ]), 0)
            run_id = json.loads(started.getvalue())["run_id"]
            requested = io.StringIO()
            with redirect_stdout(requested):
                self.assertEqual(main([
                    "--state-root", str(state_root), "host-execution-request", "--run-id", run_id,
                    "--artifact-file", str(project / "skill.md"), "--observed-snapshot-hash", SNAPSHOT,
                ]), 0)
            external_command = json.loads(requested.getvalue())["external_command"]
            self.assertTrue(external_command.startswith("nlp-refinement-host review --allow-model-inference"))


if __name__ == "__main__":
    unittest.main()

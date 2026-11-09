from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nlp_refinement.refine import build_parser


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "a" * 64


class HostExecutionBoundaryTests(unittest.TestCase):
    def test_normal_command_has_no_host_review_entrypoint(self) -> None:
        parser = build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "host-review",
                    "--run-id", "run_1234567890ab",
                    "--artifact-file", "artifact.md",
                    "--observed-snapshot-hash", SNAPSHOT,
                ]
            )

    def test_core_orchestrator_does_not_import_the_app_server_client(self) -> None:
        coordinator = (ROOT / "nlp_refinement" / "m8_review_coordinator.py").read_text(encoding="utf-8")
        normal_cli = (ROOT / "nlp_refinement" / "refine.py").read_text(encoding="utf-8")

        self.assertNotIn("AppServerClient", coordinator)
        self.assertNotIn("subprocess", coordinator)
        self.assertNotIn("host-review", normal_cli)
        self.assertNotIn("host_executor", normal_cli)

    def test_external_host_cli_requires_explicit_model_inference_consent(self) -> None:
        from nlp_refinement.host_refine import build_parser as build_host_parser, main, review

        parser = build_host_parser()
        args = parser.parse_args(
            [
                "review",
                "--run-id", "run_1234567890ab",
                "--artifact-file", "artifact.md",
                "--observed-snapshot-hash", SNAPSHOT,
            ]
        )

        self.assertFalse(args.allow_model_inference)
        with patch("nlp_refinement.host_refine.M8ReviewCoordinator") as coordinator:
            with self.assertRaisesRegex(ValueError, "--allow-model-inference"):
                review(args, object())
        coordinator.assert_not_called()

        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            state_root = Path(directory) / "state-root"
            code = main(
                [
                    "--state-root", str(state_root), "review",
                    "--run-id", "run_1234567890ab", "--artifact-file", "artifact.md",
                    "--observed-snapshot-hash", SNAPSHOT,
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse((state_root / "state.db").exists())

    def test_normal_cli_emits_a_no_model_host_execution_request(self) -> None:
        from nlp_refinement.refine import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            brief = root / "brief.md"
            brief.write_text("review this skill", encoding="utf-8")
            state_root = root / "state"
            started = io.StringIO()
            with redirect_stdout(started):
                self.assertEqual(
                    main(
                        [
                            "--state-root", str(state_root), "start", "--project-dir", str(project),
                            "--redacted-brief-file", str(brief), "--snapshot-hash", SNAPSHOT,
                        ]
                    ),
                    0,
                )
            start_result = json.loads(started.getvalue())
            run_id = start_result["run_id"]
            requested = io.StringIO()
            with redirect_stdout(requested):
                self.assertEqual(
                    main(
                        [
                            "--state-root", str(state_root), "host-execution-request", "--run-id", run_id,
                            "--artifact-file", str(project / "skill.md"), "--observed-snapshot-hash", SNAPSHOT,
                        ]
                    ),
                    0,
                )
            result = json.loads(requested.getvalue())
            self.assertEqual(result["disposition"], "HOST_EXECUTOR_REQUIRED")
            self.assertEqual(result["event_count"], start_result["event_count"])
            self.assertEqual(
                result["execution_request"],
                {
                    "run_id": run_id,
                    "artifact_file": str(project / "skill.md"),
                    "observed_snapshot_hash": SNAPSHOT,
                },
            )
            self.assertTrue(result["no_model_call_attestation"])
            self.assertTrue(result["no_target_write_attestation"])


if __name__ == "__main__":
    unittest.main()

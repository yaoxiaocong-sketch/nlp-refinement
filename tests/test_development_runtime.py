from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from nlp_refinement.development_runtime import (
    DevelopmentRuntimeError,
    TUI_NATIVE_INTERFACE,
    TUI_NATIVE_REQUIRED_COMMANDS,
    resolve_development_runtime,
    runtime_identity,
)
from nlp_refinement.version import CLI_VERSION
from nlp_refinement.refine import main


ROOT = Path(__file__).resolve().parents[1]


class DevelopmentRuntimeTests(unittest.TestCase):

    def test_development_runtime_document_lists_every_required_command(self) -> None:
        document = (ROOT / "docs/DEVELOPMENT_RUNTIME_RESOLUTION.md").read_text(encoding="utf-8")
        for command in TUI_NATIVE_REQUIRED_COMMANDS:
            self.assertIn(f'"{command}"', document)

    def test_tui_runtime_identity_requires_each_stateful_workflow_gate(self) -> None:
        self.assertTrue(
            {
                "resume",
                "tui-create-transient-root",
                "tui-record-review",
                "tui-cleanup-transient-root",
                "patch-proposal",
            }.issubset(TUI_NATIVE_REQUIRED_COMMANDS)
        )
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.executable = self.root / "venv" / "bin" / "nlp-refinement"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("fixture", encoding="utf-8")
        self.descriptor = self.root / "runtime.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_descriptor(self, **overrides: object) -> Path:
        descriptor: dict[str, object] = {
            "schema_version": "nlp-refinement-development-runtime/v1",
            "executable": str(self.executable),
            "state_root": str(self.root / "state"),
            "wheel_sha256": hashlib.sha256(b"fixture wheel").hexdigest(),
            "expected_identity": {
                "distribution_name": "nlp-refinement-cli",
                "distribution_version": CLI_VERSION,
                "interface_version": TUI_NATIVE_INTERFACE,
                "commands": list(TUI_NATIVE_REQUIRED_COMMANDS),
            },
        }
        descriptor.update(overrides)
        self.descriptor.write_text(json.dumps(descriptor), encoding="utf-8")
        return self.descriptor

    def test_runtime_identity_advertises_a_machine_readable_tui_native_contract(self) -> None:
        identity = runtime_identity(self.executable)

        self.assertEqual(identity["schema_version"], "nlp-refinement-runtime-identity/v1")
        self.assertEqual(identity["distribution_name"], "nlp-refinement-cli")
        self.assertEqual(identity["distribution_version"], CLI_VERSION)
        self.assertEqual(identity["interface_version"], TUI_NATIVE_INTERFACE)
        self.assertEqual(identity["executable"], str(self.executable.resolve()))
        self.assertIn("runtime-identity", identity["commands"])
        self.assertEqual(identity["commands"], list(TUI_NATIVE_REQUIRED_COMMANDS))

    def test_resolver_binds_the_descriptor_to_the_exact_cli_path_and_capabilities(self) -> None:
        resolved = resolve_development_runtime(
            self.write_descriptor(),
            invoked_executable=self.executable,
            identity=runtime_identity(self.executable),
        )

        self.assertEqual(resolved["executable"], str(self.executable.resolve()))
        self.assertEqual(resolved["state_root"], str((self.root / "state").resolve()))
        self.assertEqual(resolved["wheel_sha256"], hashlib.sha256(b"fixture wheel").hexdigest())
        self.assertTrue(resolved["no_model_call_attestation"])
        self.assertTrue(resolved["no_target_write_attestation"])

    def test_resolver_stops_when_the_descriptor_path_does_not_match_the_invoked_cli(self) -> None:
        other = self.root / "other" / "nlp-refinement"
        other.parent.mkdir()
        other.write_text("other", encoding="utf-8")

        with self.assertRaisesRegex(DevelopmentRuntimeError, "CLI_PATH_MISMATCH"):
            resolve_development_runtime(
                self.write_descriptor(),
                invoked_executable=other,
                identity=runtime_identity(other),
            )

    def test_resolver_stops_when_the_cli_omits_a_required_tui_command(self) -> None:
        identity = runtime_identity(self.executable)
        identity["commands"] = list(TUI_NATIVE_REQUIRED_COMMANDS[:-1])

        with self.assertRaisesRegex(DevelopmentRuntimeError, "CLI_CAPABILITY_MISMATCH"):
            resolve_development_runtime(
                self.write_descriptor(),
                invoked_executable=self.executable,
                identity=identity,
            )

    def test_resolver_rejects_undeclared_descriptor_fields(self) -> None:
        with self.assertRaisesRegex(DevelopmentRuntimeError, "DESCRIPTOR_FIELDS_INVALID"):
            resolve_development_runtime(
                self.write_descriptor(unexpected="not allowed"),
                invoked_executable=self.executable,
                identity=runtime_identity(self.executable),
            )

    def test_resolver_rejects_a_relative_state_root(self) -> None:
        with self.assertRaisesRegex(DevelopmentRuntimeError, "DESCRIPTOR_STATE_ROOT_INVALID"):
            resolve_development_runtime(
                self.write_descriptor(state_root="state"),
                invoked_executable=self.executable,
                identity=runtime_identity(self.executable),
            )

    def test_stateless_runtime_commands_do_not_initialize_a_ledger(self) -> None:
        state_root = self.root / "state"
        identity_output = io.StringIO()
        with patch("nlp_refinement.refine.sys.argv", [str(self.executable)]), redirect_stdout(identity_output):
            self.assertEqual(main(["--state-root", str(state_root), "runtime-identity"]), 0)
        self.assertEqual(json.loads(identity_output.getvalue())["command"], "runtime-identity")

        resolution_output = io.StringIO()
        with patch("nlp_refinement.refine.sys.argv", [str(self.executable)]), redirect_stdout(resolution_output):
            self.assertEqual(
                main(["--state-root", str(state_root), "runtime-resolve", "--descriptor", str(self.write_descriptor())]),
                0,
            )
        rendered = json.loads(resolution_output.getvalue())
        self.assertEqual(rendered["disposition"], "RESOLVED")
        self.assertEqual(rendered["resolved_cli"]["executable"], str(self.executable.resolve()))
        self.assertFalse((state_root / "state.db").exists())

    def test_runtime_resolve_reports_path_mismatch_without_initializing_a_ledger(self) -> None:
        state_root = self.root / "state"
        other = self.root / "other" / "nlp-refinement"
        other.parent.mkdir()
        other.write_text("other", encoding="utf-8")
        stderr = io.StringIO()

        with patch("nlp_refinement.refine.sys.argv", [str(other)]), redirect_stderr(stderr):
            self.assertEqual(
                main(["--state-root", str(state_root), "runtime-resolve", "--descriptor", str(self.write_descriptor())]),
                2,
            )

        self.assertIn("CLI_PATH_MISMATCH", stderr.getvalue())
        self.assertFalse((state_root / "state.db").exists())

    def test_runtime_identity_rejects_module_execution_without_initializing_a_ledger(self) -> None:
        state_root = self.root / "state"
        source_module = self.root / "refine.py"
        source_module.write_text("fixture", encoding="utf-8")
        stderr = io.StringIO()

        with patch("nlp_refinement.refine.sys.argv", [str(source_module)]), redirect_stderr(stderr):
            self.assertEqual(main(["--state-root", str(state_root), "runtime-identity"]), 2)

        self.assertIn("requires the nlp-refinement console script", stderr.getvalue())
        self.assertFalse((state_root / "state.db").exists())

    def test_tui_skill_requires_an_explicit_development_descriptor_argument(self) -> None:
        skill = (Path(__file__).resolve().parents[1] / ".agents/skills/nlp-review-propose/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("--development-runtime-descriptor /absolute/path", skill)
        self.assertIn("`start` or `resume` request", skill)
        self.assertNotIn("NLP_REFINEMENT_DEVELOPMENT_RUNTIME_DESCRIPTOR", skill)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from nlp_refinement.host_client import HostResult
from nlp_refinement.host_executor import AppServerHostExecutor


class _FakeClient:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.roles: list[str] = []

    def __enter__(self) -> "_FakeClient":
        self.entered = True
        return self

    def __exit__(self, *_: object) -> None:
        self.exited = True

    def run_read_only_role(self, **kwargs: object) -> HostResult:
        role = kwargs["role"]
        assert isinstance(role, str)
        self.roles.append(role)
        return HostResult(role, f"thread-{role}", f"turn-{role}", "{}", (), "read-only")


class AppServerHostExecutorTests(unittest.TestCase):
    def test_owns_one_client_connection_for_both_ordered_roles(self) -> None:
        client = _FakeClient()
        executor = AppServerHostExecutor(client_factory=lambda: client)  # type: ignore[arg-type]

        with executor:
            for role in ("reviewer", "proposal"):
                result = executor.run_read_only_role(
                    role=role,
                    cwd=Path(tempfile.gettempdir()),
                    developer_instructions="fixture",
                    prompt="fixture",
                    output_schema={},
                )
                self.assertEqual(result.role, role)

        self.assertTrue(client.entered)
        self.assertTrue(client.exited)
        self.assertEqual(client.roles, ["reviewer", "proposal"])

    def test_refuses_role_execution_before_the_executor_owns_a_connection(self) -> None:
        executor = AppServerHostExecutor(client_factory=_FakeClient)  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "must be entered"):
            executor.run_read_only_role(
                role="reviewer",
                cwd=Path(tempfile.gettempdir()),
                developer_instructions="fixture",
                prompt="fixture",
                output_schema={},
            )


if __name__ == "__main__":
    unittest.main()

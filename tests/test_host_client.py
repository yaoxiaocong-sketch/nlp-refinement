from __future__ import annotations

import io
import unittest
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock, patch

from nlp_refinement.host_client import (
    MAX_JSONL_LINE_BYTES,
    AppServerClient,
    HostProtocolError,
    _initialize_error_code,
    _host_version_label,
)


class ScriptedClient(AppServerClient):
    """A no-process JSON-RPC transcript used to test the stdlib protocol adapter."""

    def __init__(self, messages: list[dict[str, Any]], *, clock_ms: Callable[[], int] | None = None) -> None:
        super().__init__(timeout_seconds=1, clock_ms=clock_ms)
        self.messages = messages
        self.sent: list[dict[str, Any]] = []

    def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def _read_one(self, deadline: float) -> dict[str, Any]:
        del deadline
        return self.messages.pop(0)


class TimeoutAfterStartClient(ScriptedClient):
    def _read_one(self, deadline: float) -> dict[str, Any]:
        if self.messages:
            return self.messages.pop(0)
        raise HostProtocolError("timed out reading app-server output")


class TransportClosedAfterStartClient(ScriptedClient):
    def _read_one(self, deadline: float) -> dict[str, Any]:
        if self.messages:
            return self.messages.pop(0)
        raise HostProtocolError("app-server closed its output")


class AppServerClientTests(unittest.TestCase):
    def test_binary_jsonl_framer_consumes_buffered_lines_before_waiting_for_more_bytes(self) -> None:
        client = AppServerClient(timeout_seconds=1)
        client._stdout_buffer = b'{"id":1,"result":{}}\n{"id":2,"result":{}}\n'
        self.assertEqual(client._take_buffered_message()["id"], 1)
        self.assertEqual(client._take_buffered_message()["id"], 2)
        self.assertIsNone(client._take_buffered_message())

    def test_binary_jsonl_framer_rejects_an_oversized_unterminated_line(self) -> None:
        client = AppServerClient(timeout_seconds=1)
        client._stdout_buffer = b"x" * (MAX_JSONL_LINE_BYTES + 1)
        with self.assertRaisesRegex(HostProtocolError, "oversized JSONL line"):
            client._take_buffered_message()

    def test_binary_jsonl_reader_drains_a_second_buffered_message_without_waiting_for_fd(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as output:
                os.write(write_fd, b'{"id":1,"result":{}}\n{"id":2,"result":{}}\n')
                client = AppServerClient(timeout_seconds=1)
                client._process = SimpleNamespace(stdout=output, stderr=None, stdin=None)
                deadline = time.monotonic() + 0.1
                self.assertEqual(client._read_one(deadline)["id"], 1)
                self.assertEqual(client._read_one(deadline)["id"], 2)
        finally:
            os.close(write_fd)

    def test_each_role_receipt_has_only_its_own_notifications(self) -> None:
        client = ScriptedClient(
            [
                {"id": 1, "result": {"thread": {"id": "thread-review"}}},
                {"method": "thread/started", "params": {"threadId": "thread-review"}},
                {"id": 2, "result": {"turn": {"id": "turn-review"}}},
                {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "review-json"}}},
                {"method": "turn/completed", "params": {"threadId": "thread-review", "turn": {"id": "turn-review", "status": "completed"}}},
                {"id": 3, "result": {"thread": {"id": "thread-proposal"}}},
                {"method": "thread/started", "params": {"threadId": "thread-proposal"}},
                {"id": 4, "result": {"turn": {"id": "turn-proposal"}}},
                {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "proposal-json"}}},
                {"method": "turn/completed", "params": {"threadId": "thread-proposal", "turn": {"id": "turn-proposal", "status": "completed"}}},
            ]
        )
        client._events.append({"method": "preexisting-event"})
        schema = {"type": "object"}

        review = client.run_read_only_role(
            role="reviewer", cwd=Path.cwd(), developer_instructions="review", prompt="input", output_schema=schema
        )
        proposal = client.run_read_only_role(
            role="proposal", cwd=Path.cwd(), developer_instructions="proposal", prompt="input", output_schema=schema
        )

        self.assertEqual((review.thread_id, review.raw_output), ("thread-review", "review-json"))
        self.assertEqual((review.turn_status, review.model), ("completed", "unreported"))
        self.assertEqual((proposal.thread_id, proposal.raw_output), ("thread-proposal", "proposal-json"))
        self.assertFalse(any(event["method"] == "preexisting-event" for event in review.events))
        self.assertFalse(any(event["method"] == "thread/started" and event["params"]["threadId"] == "thread-review" for event in proposal.events))
        self.assertTrue(all(call["method"] in {"thread/start", "turn/start"} for call in client.sent))
        self.assertTrue(all(call["params"].get("sandbox", "read-only") == "read-only" for call in client.sent))

    def test_failed_turn_is_not_accepted_as_a_review_result(self) -> None:
        client = ScriptedClient(
            [
                {"id": 1, "result": {"thread": {"id": "thread-review"}}},
                {"id": 2, "result": {"turn": {"id": "turn-review"}}},
                {"method": "turn/completed", "params": {"threadId": "thread-review", "turn": {"id": "turn-review", "status": "failed", "error": {"code": "fixture_failure"}}}},
            ]
        )
        with self.assertRaisesRegex(HostProtocolError, "error_code='fixture_failure'") as raised:
            client.run_read_only_role(
                role="reviewer", cwd=Path.cwd(), developer_instructions="review", prompt="input", output_schema={"type": "object"}
            )
        attempt = raised.exception.attempt
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual((attempt.role, attempt.thread_id, attempt.turn_id), ("reviewer", "thread-review", "turn-review"))
        self.assertEqual((attempt.turn_status, attempt.error_code), ("failed", "fixture_failure"))
        self.assertEqual(attempt.event_methods, ("turn/completed",))
        assert attempt.timing is not None
        self.assertEqual(attempt.timing.failure_stage, "awaiting_terminal")
        self.assertIsNotNone(attempt.timing.terminal_ms)

    def test_timeout_before_host_result_has_a_bounded_attempt_receipt(self) -> None:
        ticks = iter((0, 10, 20, 30))
        client = TimeoutAfterStartClient(
            [
                {"id": 1, "result": {"thread": {"id": "thread-review", "model": "fixture-model"}}},
                {"id": 2, "result": {"turn": {"id": "turn-review"}}},
            ],
            clock_ms=lambda: next(ticks),
        )
        with self.assertRaises(HostProtocolError) as raised:
            client.run_read_only_role(
                role="reviewer", cwd=Path.cwd(), developer_instructions="review", prompt="input", output_schema={"type": "object"}
            )
        attempt = raised.exception.attempt
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual((attempt.role, attempt.thread_id, attempt.turn_id), ("reviewer", "thread-review", "turn-review"))
        self.assertEqual((attempt.turn_status, attempt.error_code), ("unobserved-timeout", "client_timeout"))
        assert attempt.timing is not None
        self.assertEqual(
            attempt.timing.as_dict(),
            {
                "failure_stage": "awaiting_first_agent_message",
                "deadline_ms": 1000,
                "initialized_ms": None,
                "role_started_ms": 10,
                "thread_started_ms": 20,
                "turn_started_ms": 30,
                "first_agent_message_ms": None,
                "terminal_ms": None,
            },
        )

    def test_timeout_after_first_agent_message_identifies_terminal_wait(self) -> None:
        ticks = iter((0, 10, 20, 30, 40))
        client = TimeoutAfterStartClient(
            [
                {"id": 1, "result": {"thread": {"id": "thread-review"}}},
                {"id": 2, "result": {"turn": {"id": "turn-review"}}},
                {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "partial"}}},
            ],
            clock_ms=lambda: next(ticks),
        )

        with self.assertRaises(HostProtocolError) as raised:
            client.run_read_only_role(
                role="reviewer", cwd=Path.cwd(), developer_instructions="review", prompt="input", output_schema={"type": "object"}
            )

        attempt = raised.exception.attempt
        assert attempt is not None and attempt.timing is not None
        self.assertEqual(attempt.timing.failure_stage, "awaiting_terminal")
        self.assertEqual(attempt.timing.first_agent_message_ms, 40)

    def test_closed_stdio_after_turn_start_is_a_transport_read_failure(self) -> None:
        client = TransportClosedAfterStartClient(
            [
                {"id": 1, "result": {"thread": {"id": "thread-review"}}},
                {"id": 2, "result": {"turn": {"id": "turn-review"}}},
            ]
        )

        with self.assertRaises(HostProtocolError) as raised:
            client.run_read_only_role(
                role="reviewer", cwd=Path.cwd(), developer_instructions="review", prompt="input", output_schema={"type": "object"}
            )

        attempt = raised.exception.attempt
        assert attempt is not None and attempt.timing is not None
        self.assertEqual((attempt.error_code, attempt.timing.failure_stage), ("transport_closed", "transport_read"))

    def test_initialize_failures_have_stable_host_capability_codes(self) -> None:
        self.assertEqual(
            _initialize_error_code(HostProtocolError("app-server closed its output")),
            "host_initialize_transport_closed",
        )
        self.assertEqual(
            _initialize_error_code(HostProtocolError("timed out reading app-server output")),
            "host_initialize_timeout",
        )
        self.assertEqual(
            _initialize_error_code(HostProtocolError("app-server initialize failed: {'code': 1}")),
            "host_initialize_rejected",
        )
        self.assertEqual(
            _initialize_error_code(HostProtocolError("app-server emitted invalid JSON")),
            "host_initialize_protocol_error",
        )

    def test_initialize_transport_close_records_host_capability_code(self) -> None:
        process = Mock()
        process.stdin = io.BytesIO()
        process.wait.return_value = 0
        client = AppServerClient(command=("fixture-app-server",), timeout_seconds=1)

        with patch("nlp_refinement.host_client.subprocess.Popen", return_value=process), \
             patch.object(client, "_host_version", return_value="fixture 1.0"), \
             patch.object(client, "_read_one", side_effect=HostProtocolError("app-server closed its output")):
            with self.assertRaises(HostProtocolError) as raised:
                client.__enter__()

        attempt = raised.exception.attempt
        assert attempt is not None and attempt.timing is not None
        self.assertEqual(attempt.error_code, "host_initialize_transport_closed")
        self.assertEqual(attempt.timing.failure_stage, "initialize")
        self.assertEqual(attempt.thread_id, None)
        self.assertEqual(attempt.turn_id, None)

    def test_host_version_rejects_a_non_version_error_line(self) -> None:
        self.assertEqual(_host_version_label("file:///opt/codex/bin/codex.js:107"), "unreported")
        self.assertEqual(_host_version_label("codex-cli 0.165.0"), "codex-cli 0.165.0")

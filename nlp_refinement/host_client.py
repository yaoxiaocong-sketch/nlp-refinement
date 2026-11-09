"""Minimal stdlib client for the local ``codex app-server`` JSON-RPC transport.

This module intentionally owns no refinement state.  It starts independent,
read-only Codex *threads* and returns the host-provided thread/turn identifiers
plus the unmodified final agent message.  The Coordinator decides whether that
message is a valid review or proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from typing import Any, Callable, Iterable


_SAFE_HOST_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_EVENT_METHOD = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
MAX_JSONL_LINE_BYTES = 1024 * 1024
_FAILURE_STAGES = frozenset({
    "initialize",
    "thread_start",
    "turn_start",
    "awaiting_first_agent_message",
    "awaiting_terminal",
    "transport_read",
})


@dataclass(frozen=True)
class HostAttemptTiming:
    """Bounded, content-free timings relative to one app-server connection."""

    failure_stage: str
    deadline_ms: int
    initialized_ms: int | None
    role_started_ms: int | None
    thread_started_ms: int | None
    turn_started_ms: int | None
    first_agent_message_ms: int | None
    terminal_ms: int | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "failure_stage": self.failure_stage,
            "deadline_ms": self.deadline_ms,
            "initialized_ms": self.initialized_ms,
            "role_started_ms": self.role_started_ms,
            "thread_started_ms": self.thread_started_ms,
            "turn_started_ms": self.turn_started_ms,
            "first_agent_message_ms": self.first_agent_message_ms,
            "terminal_ms": self.terminal_ms,
        }


@dataclass
class _RoleTiming:
    initialized_ms: int | None
    role_started_ms: int | None = None
    thread_started_ms: int | None = None
    turn_started_ms: int | None = None
    first_agent_message_ms: int | None = None
    terminal_ms: int | None = None


@dataclass(frozen=True)
class HostAttempt:
    """Bounded evidence available even when a HostResult cannot be formed."""

    role: str
    thread_id: str | None
    turn_id: str | None
    turn_status: str | None
    error_code: str | None
    event_count: int
    event_methods: tuple[str, ...]
    host_version: str
    model: str
    timeout_seconds: float
    effective_sandbox: str | None
    timing: HostAttemptTiming | None = None


class HostProtocolError(RuntimeError):
    """The local Codex host did not supply the promised JSON-RPC evidence."""

    def __init__(self, message: str, *, attempt: HostAttempt | None = None) -> None:
        super().__init__(message)
        self.attempt = attempt


@dataclass(frozen=True)
class HostResult:
    role: str
    thread_id: str
    turn_id: str | None
    raw_output: str
    events: tuple[dict[str, Any], ...]
    effective_sandbox: str | None
    turn_status: str = "completed"
    host_version: str = "unreported"
    model: str = "unreported"
    timeout_seconds: float = 0.0


class AppServerClient:
    """One short-lived app-server connection, implemented with Python stdlib only."""

    def __init__(
        self,
        command: Iterable[str] = ("codex", "app-server", "--listen", "stdio://"),
        *,
        timeout_seconds: float = 120.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._events: list[dict[str, Any]] = []
        self._stdout_buffer = b""
        self.host_version = "unreported"
        self._clock_ms = clock_ms or (lambda: int(time.monotonic() * 1000))
        self._connection_started_ms = self._clock_ms()
        self._initialized_ms: int | None = None

    def __enter__(self) -> "AppServerClient":
        self._connection_started_ms = self._clock_ms()
        self._initialized_ms = None
        self.host_version = self._host_version()
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "nlp-refinement-m8",
                        "title": "NLP-refinement M8 Coordinator",
                        "version": "0.6.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            self._initialized_ms = self._elapsed_ms()
            self.notify("initialized", {})
        except HostProtocolError as error:
            self._stop_process()
            raise HostProtocolError(
                str(error),
                attempt=error.attempt or self._attempt(
                    "unassigned", None, None, _unobserved_status(error), _initialize_error_code(error), 0, "unreported", None,
                    _RoleTiming(self._initialized_ms), "initialize",
                ),
            ) from error
        except OSError as error:
            self._stop_process()
            raise HostProtocolError(
                "app-server launch failed",
                attempt=self._attempt(
                    "unassigned", None, None, "unobserved-before-thread", "host_launch_failed", 0, "unreported", None,
                    _RoleTiming(self._initialized_ms), "initialize",
                ),
            ) from error
        return self

    def __exit__(self, *_: object) -> None:
        self._stop_process()

    @property
    def event_count(self) -> int:
        """Expose only an event boundary for an opt-in diagnostic executor."""
        return len(self._events)

    def events_after(self, event_start: int) -> list[dict[str, Any]]:
        """Return in-memory events after one role begins; never persists them."""
        return self._events[event_start:]

    def _stop_process(self) -> None:
        if self._process is None:
            return
        if self._process.stdin:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            message = self._read_one(deadline)
            if message.get("id") != request_id:
                self._record_notification_or_reject_request(message)
                continue
            if "error" in message:
                raise HostProtocolError(f"app-server {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise HostProtocolError(f"app-server {method} returned no object result")
            return result
        raise HostProtocolError(f"timed out waiting for app-server {method}")

    def run_read_only_role(
        self,
        *,
        role: str,
        cwd: Path,
        developer_instructions: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> HostResult:
        event_start = len(self._events)
        thread_id: str | None = None
        turn_id: str | None = None
        effective_sandbox: str | None = None
        model = "unreported"
        timing = _RoleTiming(self._initialized_ms, role_started_ms=self._elapsed_ms())
        stage = "thread_start"
        try:
            started = self.request(
                "thread/start",
                {
                    "cwd": str(cwd.resolve(strict=True)),
                    "sandbox": "read-only",
                    "approvalPolicy": "untrusted",
                    "developerInstructions": developer_instructions,
                    "serviceName": "nlp-refinement-m8",
                    "threadSource": "nlp-refinement-m8",
                },
            )
            thread = started.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise HostProtocolError("app-server thread/start omitted thread.id")
            thread_id = thread["id"]
            timing.thread_started_ms = self._elapsed_ms()
            effective_sandbox = started.get("sandbox") if isinstance(started.get("sandbox"), str) else None
            model = _host_label(thread.get("model")) or _host_label(started.get("model")) or "unreported"
            if effective_sandbox not in {None, "read-only"}:
                raise HostProtocolError(f"app-server did not retain read-only sandbox: {effective_sandbox}")

            stage = "turn_start"
            turn = self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "outputSchema": output_schema,
                    "approvalPolicy": "untrusted",
                },
            )
            turn_value = turn.get("turn")
            turn_id = turn_value.get("id") if isinstance(turn_value, dict) and isinstance(turn_value.get("id"), str) else None
            timing.turn_started_ms = self._elapsed_ms()
            stage = "awaiting_completion"
            raw_output = self._wait_for_final_message(thread_id, turn_id, timing)
        except HostProtocolError as error:
            prior = error.attempt
            failure_stage = self._failure_stage(stage, error, timing)
            raise HostProtocolError(
                str(error),
                attempt=self._attempt(
                    role,
                    thread_id,
                    turn_id,
                    prior.turn_status if prior is not None else _unobserved_status(error),
                    prior.error_code if prior is not None else _limited_error_code(error),
                    event_start,
                    model,
                    effective_sandbox,
                    timing,
                    failure_stage,
                ),
            ) from error
        return HostResult(
            role, thread_id, turn_id, raw_output, tuple(self._events[event_start:]), effective_sandbox,
            "completed", self.host_version, model, self.timeout_seconds,
        )

    def _wait_for_final_message(self, thread_id: str, turn_id: str | None, timing: _RoleTiming) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        messages: list[str] = []
        while time.monotonic() < deadline:
            message = self._read_one(deadline)
            if "id" in message:
                self._record_notification_or_reject_request(message)
                continue
            self._events.append(message)
            params = message.get("params")
            if isinstance(params, dict):
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    if timing.first_agent_message_ms is None:
                        timing.first_agent_message_ms = self._elapsed_ms()
                    messages.append(item["text"])
                if message.get("method") == "turn/completed":
                    observed_thread = params.get("threadId")
                    observed_turn = params.get("turn", {})
                    observed_turn_id = observed_turn.get("id") if isinstance(observed_turn, dict) else None
                    if observed_thread == thread_id and (turn_id is None or observed_turn_id in {None, turn_id}):
                        timing.terminal_ms = self._elapsed_ms()
                        turn_status = observed_turn.get("status") if isinstance(observed_turn, dict) else None
                        if turn_status != "completed":
                            error_code = None
                            if isinstance(observed_turn, dict) and isinstance(observed_turn.get("error"), dict):
                                error_code = _host_label(observed_turn["error"].get("code"))
                            raise HostProtocolError(
                                "app-server turn did not complete successfully: "
                                f"status={turn_status!r}, error_code={error_code!r}",
                                attempt=HostAttempt(
                                    role="unknown",
                                    thread_id=thread_id,
                                    turn_id=turn_id,
                                    turn_status=_host_label(turn_status),
                                    error_code=error_code,
                                    event_count=0,
                                    event_methods=(),
                                    host_version="unreported",
                                    model="unreported",
                                    timeout_seconds=self.timeout_seconds,
                                    effective_sandbox=None,
                                ),
                            )
                        if not messages:
                            raise HostProtocolError("app-server completed a turn without an agentMessage")
                        return messages[-1]
        raise HostProtocolError(
            "timed out waiting for app-server turn completion",
        )

    def _elapsed_ms(self) -> int:
        return max(0, self._clock_ms() - self._connection_started_ms)

    def _failure_stage(self, stage: str, error: HostProtocolError, timing: _RoleTiming) -> str:
        error_code = _limited_error_code(error)
        if error_code in {"transport_closed", "invalid_json_rpc"}:
            return "transport_read"
        if stage == "awaiting_completion":
            if timing.terminal_ms is not None:
                return "awaiting_terminal"
            return "awaiting_terminal" if timing.first_agent_message_ms is not None else "awaiting_first_agent_message"
        return stage

    def _attempt(
        self,
        role: str,
        thread_id: str | None,
        turn_id: str | None,
        turn_status: str | None,
        error_code: str | None,
        event_start: int,
        model: str,
        effective_sandbox: str | None,
        timing: _RoleTiming,
        failure_stage: str,
    ) -> HostAttempt:
        events = self._events[event_start:]
        return HostAttempt(
            role=role,
            thread_id=thread_id,
            turn_id=turn_id,
            turn_status=turn_status,
            error_code=error_code,
            event_count=len(events),
            event_methods=tuple(sorted({method for event in events if (method := _event_method(event.get("method")))}))[:16],
            host_version=self.host_version,
            model=model,
            timeout_seconds=self.timeout_seconds,
            effective_sandbox=effective_sandbox,
            timing=HostAttemptTiming(
                failure_stage=failure_stage if failure_stage in _FAILURE_STAGES else "transport_read",
                deadline_ms=max(0, int(round(self.timeout_seconds * 1000))),
                initialized_ms=timing.initialized_ms,
                role_started_ms=timing.role_started_ms,
                thread_started_ms=timing.thread_started_ms,
                turn_started_ms=timing.turn_started_ms,
                first_agent_message_ms=timing.first_agent_message_ms,
                terminal_ms=timing.terminal_ms,
            ),
        )

    def _host_version(self) -> str:
        try:
            completed = subprocess.run(
                [self.command[0], "--version"], capture_output=True, text=True, timeout=min(self.timeout_seconds, 10), check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unreported"
        raw = (completed.stdout or completed.stderr).strip()
        return _host_version_label(raw.splitlines()[0] if raw else None)

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise HostProtocolError("app-server process is not running")
        self._process.stdin.write((json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        self._process.stdin.flush()

    def _read_one(self, deadline: float) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise HostProtocolError("app-server process is not running")
        value = self._take_buffered_message()
        if value is not None:
            return value
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostProtocolError("timed out reading app-server output")
            readable, _, _ = select.select([self._process.stdout.fileno()], [], [], remaining)
            if not readable:
                raise HostProtocolError("timed out reading app-server output")
            chunk = os.read(self._process.stdout.fileno(), 64 * 1024)
            if not chunk:
                raise HostProtocolError("app-server closed its output")
            self._stdout_buffer += chunk
            value = self._take_buffered_message()
            if value is not None:
                return value

    def _take_buffered_message(self) -> dict[str, Any] | None:
        newline = self._stdout_buffer.find(b"\n")
        if newline < 0:
            if len(self._stdout_buffer) > MAX_JSONL_LINE_BYTES:
                raise HostProtocolError("app-server emitted an oversized JSONL line")
            return None
        if newline > MAX_JSONL_LINE_BYTES:
            raise HostProtocolError("app-server emitted an oversized JSONL line")
        line, self._stdout_buffer = self._stdout_buffer[:newline], self._stdout_buffer[newline + 1:]
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HostProtocolError("app-server emitted invalid JSON") from error
        if not isinstance(value, dict):
            raise HostProtocolError("app-server emitted a non-object JSON-RPC message")
        return value

    def _record_notification_or_reject_request(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            raise HostProtocolError(f"unsupported app-server request: {message['method']}")
        self._events.append(message)


def _host_label(value: object) -> str | None:
    """Permit short identifiers, never free-form host error text in receipts."""
    return value if isinstance(value, str) and _SAFE_HOST_CODE.fullmatch(value) else None


def _host_version_label(value: object) -> str:
    if not isinstance(value, str):
        return "unreported"
    compact = " ".join(value.split())
    if not compact or re.search(r"\b\d+\.\d+(?:\.\d+)?\b", compact) is None:
        return "unreported"
    return compact[:160]


def _event_method(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_EVENT_METHOD.fullmatch(value) else None


def _limited_error_code(error: HostProtocolError) -> str:
    message = str(error)
    if "timed out" in message:
        return "client_timeout"
    if "closed its output" in message:
        return "transport_closed"
    if "invalid JSON" in message or "non-object JSON-RPC" in message:
        return "invalid_json_rpc"
    return "host_protocol_error"


def _initialize_error_code(error: HostProtocolError) -> str:
    """Classify the mandatory app-server handshake without retaining host text."""
    limited_code = _limited_error_code(error)
    if limited_code == "client_timeout":
        return "host_initialize_timeout"
    if limited_code == "transport_closed":
        return "host_initialize_transport_closed"
    if str(error).startswith("app-server initialize failed:"):
        return "host_initialize_rejected"
    return "host_initialize_protocol_error"


def _unobserved_status(error: HostProtocolError) -> str:
    return "unobserved-timeout" if _limited_error_code(error) == "client_timeout" else "unobserved-before-terminal"

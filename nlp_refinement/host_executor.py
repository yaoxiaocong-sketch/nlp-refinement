"""The only production boundary that owns a local Codex app-server process."""

from __future__ import annotations

from typing import Any, Callable, Protocol, Self

from nlp_refinement.host_client import AppServerClient, HostProtocolError, HostResult


class HostExecutor(Protocol):
    """Run one or more read-only host roles on one owned host connection."""

    def __enter__(self) -> Self: ...

    def __exit__(self, *_: object) -> None: ...

    def run_read_only_role(
        self,
        *,
        role: str,
        cwd: Any,
        developer_instructions: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> HostResult: ...


class AppServerHostExecutor:
    """External-host implementation of :class:`HostExecutor`.

    Construct this class only in an explicitly invoked host-capable command or
    opt-in operational DOE.  The M8 domain coordinator receives the protocol
    above and never starts an app-server itself.
    """

    def __init__(self, client_factory: Callable[[], AppServerClient] = AppServerClient) -> None:
        self._client_factory = client_factory
        self._client: AppServerClient | None = None

    def __enter__(self) -> "AppServerHostExecutor":
        client = self._client_factory()
        client.__enter__()
        self._client = client
        return self

    def __exit__(self, *_: object) -> None:
        if self._client is not None:
            self._client.__exit__()
            self._client = None

    def run_read_only_role(
        self,
        *,
        role: str,
        cwd: Any,
        developer_instructions: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> HostResult:
        if self._client is None:
            raise RuntimeError("host executor must be entered before running a role")
        return self._client.run_read_only_role(
            role=role,
            cwd=cwd,
            developer_instructions=developer_instructions,
            prompt=prompt,
            output_schema=output_schema,
        )


class DiagnosticAppServerHostExecutor(AppServerHostExecutor):
    """DOE-only executor that provides ephemeral role-failure events to a caller."""

    def __init__(
        self,
        record_failure: Callable[[str, list[dict[str, Any]]], None],
        client_factory: Callable[[], AppServerClient] = AppServerClient,
    ) -> None:
        super().__init__(client_factory=client_factory)
        self._record_failure = record_failure

    def run_read_only_role(
        self,
        *,
        role: str,
        cwd: Any,
        developer_instructions: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> HostResult:
        if self._client is None:
            raise RuntimeError("host executor must be entered before running a role")
        event_start = self._client.event_count
        try:
            return super().run_read_only_role(
                role=role,
                cwd=cwd,
                developer_instructions=developer_instructions,
                prompt=prompt,
                output_schema=output_schema,
            )
        except HostProtocolError:
            self._record_failure(role, self._client.events_after(event_start))
            raise

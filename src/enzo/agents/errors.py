from __future__ import annotations

from enum import StrEnum


class AgentFailureKind(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    AUTHENTICATION = "AUTHENTICATION"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    PROTOCOL = "PROTOCOL"
    PROCESS = "PROCESS"


class AgentRunnerError(RuntimeError):
    """Normalized adapter failure consumed by Enzo retry policy and audit logs."""

    def __init__(
        self,
        message: str,
        *,
        kind: AgentFailureKind,
        runner_name: str | None,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.runner_name = runner_name
        self.retryable = retryable


class AgentRoutingError(AgentRunnerError):
    def __init__(self, message: str, *, runner_name: str | None = None) -> None:
        super().__init__(
            message,
            kind=AgentFailureKind.CONFIGURATION,
            runner_name=runner_name,
            retryable=False,
        )

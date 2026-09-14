from __future__ import annotations

from typing import Protocol

from .domain import AgentRequest, AgentResult, AgentRunnerSpec, ExternalFeature, OutboxMessage


class AgentRunner(Protocol):
    @property
    def spec(self) -> AgentRunnerSpec: ...

    def run(self, request: AgentRequest) -> AgentResult: ...


class TaskManagerAdapter(Protocol):
    def get_feature(self, external_feature_id: str) -> ExternalFeature: ...

    def deliver(self, message: OutboxMessage) -> str: ...

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Protocol

from ..domain import (
    AgentCapability,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRunInfo,
    AgentRunnerSpec,
    AgentWorkload,
)
from ..ports import AgentRunner
from .errors import AgentRoutingError


class AgentRoutingPolicy(Protocol):
    def runner_name_for(self, request: AgentRequest) -> str: ...


@dataclass(frozen=True)
class RunnerRoutingPolicy:
    """Deterministic runner selection; workload takes precedence over role."""

    default_runner: str
    by_workload: Mapping[AgentWorkload, str] | None = None
    by_role: Mapping[AgentRole, str] | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> RunnerRoutingPolicy:
        by_workload = {
            workload: value.strip()
            for workload in AgentWorkload
            if (value := environ.get(f"ENZO_AGENT_RUNNER_{workload.value}", "")).strip()
        }
        by_role = {
            role: value.strip()
            for role in AgentRole
            if (value := environ.get(f"ENZO_AGENT_RUNNER_ROLE_{role.value}", "")).strip()
        }
        return cls(
            default_runner=environ.get("ENZO_AGENT_DEFAULT_RUNNER", "fake").strip(),
            by_workload=by_workload,
            by_role=by_role,
        )

    def runner_name_for(self, request: AgentRequest) -> str:
        workload_routes = self.by_workload or {}
        role_routes = self.by_role or {}
        return workload_routes.get(
            request.workload,
            role_routes.get(request.role, self.default_runner),
        )


class AgentRunnerRouter:
    """Small workflow-facing facade over independently replaceable runners."""

    def __init__(
        self,
        runners: Iterable[AgentRunner],
        policy: AgentRoutingPolicy,
    ) -> None:
        registered: dict[str, AgentRunner] = {}
        for runner in runners:
            name = runner.spec.name.strip()
            if not name:
                raise ValueError("agent runner name cannot be empty")
            if name in registered:
                raise ValueError(f"duplicate agent runner name: {name}")
            registered[name] = runner
        if not registered:
            raise ValueError("at least one agent runner must be registered")
        self._runners = registered
        self._policy = policy
        if isinstance(policy, RunnerRoutingPolicy):
            self._validate_static_policy(policy)

    @property
    def spec(self) -> AgentRunnerSpec:
        capabilities = frozenset(
            capability
            for runner in self._runners.values()
            for capability in runner.spec.capabilities
        )
        return AgentRunnerSpec(
            name="router",
            kind="router",
            capabilities=capabilities,
        )

    @property
    def runner_names(self) -> tuple[str, ...]:
        return tuple(self._runners)

    def selected_runner(self, request: AgentRequest) -> AgentRunnerSpec:
        name = self._policy.runner_name_for(request)
        try:
            runner = self._runners[name]
        except KeyError as exc:
            raise AgentRoutingError(
                f"agent runner is not registered: {name}", runner_name=name
            ) from exc
        required = required_capabilities(request)
        missing = required - runner.spec.capabilities
        if missing:
            values = ", ".join(sorted(capability.value for capability in missing))
            raise AgentRoutingError(
                f"agent runner {name!r} lacks required capabilities: {values}",
                runner_name=name,
            )
        return runner.spec

    def run(self, request: AgentRequest) -> AgentResult:
        selected = self.selected_runner(request)
        result = self._runners[selected.name].run(request)
        reported = result.run_info
        run_info = AgentRunInfo(
            runner_name=selected.name,
            runner_kind=selected.kind,
            provider=reported.provider if reported else None,
            model=reported.model if reported else None,
        )
        return replace(result, run_info=run_info)

    def _validate_static_policy(self, policy: RunnerRoutingPolicy) -> None:
        configured = {policy.default_runner}
        configured.update((policy.by_workload or {}).values())
        configured.update((policy.by_role or {}).values())
        missing = configured - self._runners.keys()
        if missing:
            raise ValueError(
                "routing policy references unregistered runners: "
                + ", ".join(sorted(missing))
            )


def required_capabilities(request: AgentRequest) -> frozenset[AgentCapability]:
    if request.workload in {
        AgentWorkload.IMPLEMENTATION,
        AgentWorkload.IMPLEMENTATION_REVISION,
    }:
        return frozenset({AgentCapability.EDIT_WORKSPACE})
    return frozenset({AgentCapability.GENERATE_TEXT})


def require_run_info(result: AgentResult) -> AgentRunInfo:
    if result.run_info is None:
        raise RuntimeError("agent runner returned no run provenance")
    return result.run_info

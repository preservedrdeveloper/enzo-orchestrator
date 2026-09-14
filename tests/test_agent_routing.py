from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from conftest import Harness
from test_intent_cycle import create_ready_intent

from enzo.agents import (
    AgentFailureKind,
    AgentRoutingError,
    AgentRunnerRouter,
    RunnerRoutingPolicy,
)
from enzo.domain import (
    AgentCapability,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRunInfo,
    AgentRunnerSpec,
    AgentWorkload,
)


@dataclass
class RecordingRunner:
    spec: AgentRunnerSpec
    requests: list[AgentRequest] = field(default_factory=list)

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(
            content=f"result from {self.spec.name}",
            run_info=AgentRunInfo(
                runner_name="untrusted-reported-name",
                runner_kind="untrusted-reported-kind",
                provider="provider-from-adapter",
                model="model-from-adapter",
            ),
        )


def runner(
    name: str,
    *capabilities: AgentCapability,
) -> RecordingRunner:
    return RecordingRunner(
        AgentRunnerSpec(
            name=name,
            kind=f"{name}-adapter",
            capabilities=frozenset(capabilities),
        )
    )


def request(
    workload: AgentWorkload,
    *,
    role: AgentRole = AgentRole.FEEDBACK_RESOLVER,
) -> AgentRequest:
    return AgentRequest(
        role=role,
        workload=workload,
        project_id="project-1",
        feature_id="feature-1",
        artifact_revision_id="revision-1",
        title="Carrier filter",
        workspace_path="/worktree" if "IMPLEMENTATION" in workload.value else None,
    )


def test_router_uses_workload_then_role_then_default_and_stamps_provenance() -> None:
    default = runner(
        "default",
        AgentCapability.GENERATE_TEXT,
        AgentCapability.EDIT_WORKSPACE,
    )
    writer = runner("writer", AgentCapability.GENERATE_TEXT)
    coder = runner("coder", AgentCapability.EDIT_WORKSPACE)
    router = AgentRunnerRouter(
        [default, writer, coder],
        RunnerRoutingPolicy(
            default_runner="default",
            by_role={AgentRole.FEEDBACK_RESOLVER: "writer"},
            by_workload={AgentWorkload.IMPLEMENTATION_REVISION: "coder"},
        ),
    )

    artifact_result = router.run(request(AgentWorkload.ARTIFACT_REVISION))
    implementation_result = router.run(request(AgentWorkload.IMPLEMENTATION_REVISION))
    default_result = router.run(
        request(AgentWorkload.ARTIFACT_GENERATION, role=AgentRole.INTENT)
    )

    assert artifact_result.content == "result from writer"
    assert implementation_result.content == "result from coder"
    assert default_result.content == "result from default"
    assert implementation_result.run_info == AgentRunInfo(
        runner_name="coder",
        runner_kind="coder-adapter",
        provider="provider-from-adapter",
        model="model-from-adapter",
    )


def test_router_rejects_runner_without_required_capability_before_invocation() -> None:
    writer = runner("writer", AgentCapability.GENERATE_TEXT)
    router = AgentRunnerRouter(
        [writer],
        RunnerRoutingPolicy(default_runner="writer"),
    )

    with pytest.raises(AgentRoutingError, match="EDIT_WORKSPACE") as raised:
        router.run(request(AgentWorkload.IMPLEMENTATION))
    assert raised.value.kind is AgentFailureKind.CONFIGURATION
    assert raised.value.runner_name == "writer"
    assert raised.value.retryable is False
    assert writer.requests == []


def test_routing_policy_from_env_supports_independent_workload_routes() -> None:
    policy = RunnerRoutingPolicy.from_env(
        {
            "ENZO_AGENT_DEFAULT_RUNNER": "pi",
            "ENZO_AGENT_RUNNER_IMPLEMENTATION": "codex",
            "ENZO_AGENT_RUNNER_ROLE_SPEC": "claude",
        }
    )

    assert policy.runner_name_for(
        request(AgentWorkload.IMPLEMENTATION, role=AgentRole.CODER)
    ) == "codex"
    assert policy.runner_name_for(
        request(AgentWorkload.ARTIFACT_GENERATION, role=AgentRole.SPEC)
    ) == "claude"
    assert policy.runner_name_for(
        request(AgentWorkload.ARTIFACT_GENERATION, role=AgentRole.INTENT)
    ) == "pi"


def test_artifact_agent_run_persists_concrete_runner_provenance(harness: Harness) -> None:
    create_ready_intent(harness, external_id="PLANE-RUNNER-AUDIT")

    with harness.database.read() as connection:
        run = connection.execute(
            """SELECT role, workload, runner_name, runner_kind, status, result_json
               FROM agent_runs"""
        ).fetchone()

    assert dict(run) == {
        "role": "INTENT",
        "workload": "ARTIFACT_GENERATION",
        "runner_name": "fake",
        "runner_kind": "fake",
        "status": "SUCCEEDED",
        "result_json": "{}",
    }

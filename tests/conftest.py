from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from enzo.agents import AgentRunnerRouter, RunnerRoutingPolicy
from enzo.artifacts.store import ArtifactStore
from enzo.database import Database
from enzo.execution.commands import CommandRunner
from enzo.execution.coordinator import ExecutionCoordinator
from enzo.execution.git import GitWorkspaceManager
from enzo.execution.models import ProjectConfig
from enzo.fakes import FakeAgentRunner, FakeTaskManagerAdapter
from enzo.orchestrator import Orchestrator


@dataclass
class Harness:
    database: Database
    artifact_store: ArtifactStore
    agent: FakeAgentRunner
    agent_runner: AgentRunnerRouter
    plane: FakeTaskManagerAdapter
    orchestrator: Orchestrator
    project: ProjectConfig
    execution_coordinator: ExecutionCoordinator | None


def make_harness(
    root: Path,
    agent: FakeAgentRunner | None = None,
    project: ProjectConfig | None = None,
) -> Harness:
    database = Database(root / "state.db")
    database.initialize()
    artifact_store = ArtifactStore(root / "artifacts")
    fake_agent = agent or FakeAgentRunner()
    agent_runner = AgentRunnerRouter(
        [fake_agent],
        RunnerRoutingPolicy(default_runner=fake_agent.spec.name),
    )
    plane = FakeTaskManagerAdapter()
    configured_project = project or ProjectConfig(
        id="test-project",
        external_id="test-project",
        name="Test Project",
    )
    execution_coordinator = None
    if configured_project.execution_enabled:
        execution_coordinator = ExecutionCoordinator(
            database=database,
            project=configured_project,
            agent_runner=agent_runner,
            git=GitWorkspaceManager(
                repository_root=root / "repositories",
                worktree_root=root / "worktrees",
            ),
            commands=CommandRunner(timeout_seconds=10),
            evidence_root=root / "executions",
        )
    orchestrator = Orchestrator(
        database=database,
        artifact_store=artifact_store,
        agent_runner=agent_runner,
        task_manager=plane,
        reviewer_ids={"reviewer-1", "reviewer-2"},
        project=configured_project,
        execution_coordinator=execution_coordinator,
    )
    return Harness(
        database,
        artifact_store,
        fake_agent,
        agent_runner,
        plane,
        orchestrator,
        configured_project,
        execution_coordinator,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .agents import build_agent_runner
from .artifacts.store import ArtifactStore
from .database import Database
from .execution.commands import CommandRunner
from .execution.coordinator import ExecutionCoordinator
from .execution.git import GitWorkspaceManager
from .execution.models import ProjectConfig
from .fakes import FakeTaskManagerAdapter
from .orchestrator import Orchestrator
from .plane import PlaneProjectBinding, PlaneTaskManagerAdapter, PlaneWebhookHandler
from .ports import AgentRunner, TaskManagerAdapter


@dataclass(frozen=True)
class Container:
    database: Database
    artifact_store: ArtifactStore
    agent_runner: AgentRunner
    task_manager: TaskManagerAdapter
    orchestrator: Orchestrator
    plane_webhook: PlaneWebhookHandler | None = None
    project: ProjectConfig | None = None
    execution_coordinator: ExecutionCoordinator | None = None


def build_container(data_dir: Path | None = None) -> Container:
    root = data_dir or Path(os.environ.get("ENZO_DATA_DIR", "var"))
    database = Database(root / "state.db")
    database.initialize()
    artifact_store = ArtifactStore(root / "artifacts")
    agent_runner = build_agent_runner(os.environ, data_dir=root)
    task_manager_mode = os.environ.get("ENZO_TASK_MANAGER", "fake").strip().lower()
    plane_webhook = None
    if task_manager_mode == "fake":
        task_manager: TaskManagerAdapter = FakeTaskManagerAdapter()
    elif task_manager_mode == "plane":
        binding = PlaneProjectBinding.from_env(os.environ)
        task_manager = PlaneTaskManagerAdapter(binding)
        plane_webhook = PlaneWebhookHandler(binding)
    else:
        raise ValueError("ENZO_TASK_MANAGER must be 'fake' or 'plane'")
    task_manager_project_id = (
        os.environ.get("ENZO_PLANE_PROJECT_ID") if task_manager_mode == "plane" else None
    )
    project = ProjectConfig.from_env(
        os.environ,
        task_manager_project_id=task_manager_project_id,
    )
    execution_coordinator = None
    if project.execution_enabled:
        execution_coordinator = ExecutionCoordinator(
            database=database,
            project=project,
            agent_runner=agent_runner,
            git=GitWorkspaceManager(
                repository_root=root / "repositories",
                worktree_root=root / "worktrees",
            ),
            commands=CommandRunner(),
            evidence_root=root / "executions",
        )
    reviewers = {
        value.strip()
        for value in os.environ.get("ENZO_REVIEWERS", "reviewer-1").split(",")
        if value.strip()
    }
    orchestrator = Orchestrator(
        database=database,
        artifact_store=artifact_store,
        agent_runner=agent_runner,
        task_manager=task_manager,
        reviewer_ids=reviewers,
        project=project,
        execution_coordinator=execution_coordinator,
    )
    return Container(
        database,
        artifact_store,
        agent_runner,
        task_manager,
        orchestrator,
        plane_webhook,
        project,
        execution_coordinator,
    )

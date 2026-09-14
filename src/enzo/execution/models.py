from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from ..config import environment_flag


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]

    @classmethod
    def parse(cls, name: str, value: str | None) -> CommandSpec | None:
        if not value or not value.strip():
            return None
        argv = tuple(shlex.split(value))
        if not argv:
            return None
        return cls(name=name, argv=argv)


@dataclass(frozen=True)
class ProjectConfig:
    """Project-level repository binding; never copied onto individual features."""

    id: str
    external_id: str
    name: str
    repository_url: str | None = None
    default_branch: str = "main"
    branch_prefix: str = "ai/"
    auto_push: bool = True
    cleanup_on_done: bool = True
    commands: tuple[CommandSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.external_id.strip():
            raise ValueError("project id and external_id are required")
        if not self.default_branch.strip():
            raise ValueError("repository default branch is required")
        names = [command.name for command in self.commands]
        if len(names) != len(set(names)):
            raise ValueError("project command names must be unique")
        unknown = set(names) - {"bootstrap", "lint", "test", "build"}
        if unknown:
            raise ValueError("unknown project commands: " + ", ".join(sorted(unknown)))
        if self.repository_url:
            configured = set(names)
            missing = {"lint", "test", "build"} - configured
            if missing:
                raise ValueError(
                    "execution-enabled projects require commands: "
                    + ", ".join(sorted(missing))
                )

    @property
    def execution_enabled(self) -> bool:
        return bool(self.repository_url)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] = os.environ,
        *,
        task_manager_project_id: str | None = None,
    ) -> ProjectConfig:
        external_id = (
            environ.get("ENZO_PROJECT_ID")
            or task_manager_project_id
            or "default-project"
        )
        commands = tuple(
            command
            for command in (
                CommandSpec.parse("bootstrap", environ.get("ENZO_COMMAND_BOOTSTRAP")),
                CommandSpec.parse("lint", environ.get("ENZO_COMMAND_LINT")),
                CommandSpec.parse("test", environ.get("ENZO_COMMAND_TEST")),
                CommandSpec.parse("build", environ.get("ENZO_COMMAND_BUILD")),
            )
            if command is not None
        )
        return cls(
            id=environ.get("ENZO_PROJECT_INTERNAL_ID", external_id),
            external_id=external_id,
            name=environ.get("ENZO_PROJECT_NAME", "Enzo Project"),
            repository_url=environ.get("ENZO_REPOSITORY_URL") or None,
            default_branch=environ.get("ENZO_REPOSITORY_DEFAULT_BRANCH", "main"),
            branch_prefix=environ.get("ENZO_GIT_BRANCH_PREFIX", "ai/"),
            auto_push=environment_flag(environ, "ENZO_GIT_AUTO_PUSH", default=True),
            cleanup_on_done=environment_flag(
                environ, "ENZO_CLEANUP_ON_DONE", default=True
            ),
            commands=commands,
        )


@dataclass(frozen=True)
class PreparedWorkspace:
    checkout_path: Path
    worktree_path: Path
    base_sha: str
    branch: str


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    exit_code: int
    output: str
    duration_seconds: float

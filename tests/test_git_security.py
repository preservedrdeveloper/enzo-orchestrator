from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from enzo.execution.git import GitOperationError, GitWorkspaceManager
from enzo.execution.models import ProjectConfig


def test_git_errors_redact_credentials_embedded_in_https_remotes(monkeypatch) -> None:
    secret = "super-secret-token"

    def fail(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr=f"fatal: unable to access https://user:{secret}@git.example/repo.git",
        )

    monkeypatch.setattr("enzo.execution.git.subprocess.run", fail)

    with pytest.raises(GitOperationError) as raised:
        GitWorkspaceManager._run(
            "clone",
            "--",
            f"https://user:{secret}@git.example/repo.git",
            "/tmp/repo",
        )

    assert secret not in str(raised.value)
    assert "https://***@git.example/repo.git" in str(raised.value)


def test_cleanup_refuses_paths_outside_managed_worktree_root(tmp_path: Path) -> None:
    manager = GitWorkspaceManager(
        repository_root=tmp_path / "repositories",
        worktree_root=tmp_path / "worktrees",
    )
    project = ProjectConfig(
        id="project-1",
        external_id="plane-project-1",
        name="Project 1",
    )

    with pytest.raises(GitOperationError, match="outside the managed worktree root"):
        manager.cleanup(
            project,
            worktree_path=tmp_path / "user-owned-directory",
            force=True,
        )

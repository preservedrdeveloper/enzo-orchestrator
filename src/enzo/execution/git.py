from __future__ import annotations

import fcntl
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import PreparedWorkspace, ProjectConfig


class GitOperationError(RuntimeError):
    pass


class GitWorkspaceManager:
    """Owns Git clone, branch, worktree, commit, push, and cleanup operations."""

    def __init__(self, *, repository_root: Path, worktree_root: Path) -> None:
        # Git resolves worktree paths relative to the repository selected by
        # `-C`. Normalize at the boundary so runtime cwd can never relocate a
        # managed checkout or worktree.
        self.repository_root = repository_root.resolve()
        self.worktree_root = worktree_root.resolve()

    def prepare(
        self,
        project: ProjectConfig,
        *,
        execution_id: str,
        external_feature_id: str,
    ) -> PreparedWorkspace:
        if not project.repository_url:
            raise GitOperationError(f"project {project.id} has no repository binding")
        project_key = _safe_component(project.id)
        checkout = self.repository_root / project_key / "checkout"
        worktree = self.worktree_root / project_key / execution_id
        branch = f"{project.branch_prefix}{_branch_slug(external_feature_id)}"

        lock_path = self.repository_root / project_key / ".worktree.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(lock_path):
            return self._prepare_locked(
                project,
                checkout=checkout,
                worktree=worktree,
                branch=branch,
            )

    def _prepare_locked(
        self,
        project: ProjectConfig,
        *,
        checkout: Path,
        worktree: Path,
        branch: str,
    ) -> PreparedWorkspace:
        """Prepare while holding the per-project cross-process worktree lock."""

        if not (checkout / ".git").exists():
            checkout.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                "clone",
                "--origin",
                "origin",
                "--",
                project.repository_url,
                str(checkout),
            )
        else:
            configured_url = self._run("-C", str(checkout), "remote", "get-url", "origin")
            if configured_url.strip() != project.repository_url:
                raise GitOperationError(
                    f"repository cache remote mismatch: {configured_url.strip()!r}"
                )

        self._run("-C", str(checkout), "fetch", "--prune", "origin")
        base_ref = f"refs/remotes/origin/{project.default_branch}"
        base_sha = self._run("-C", str(checkout), "rev-parse", "--verify", base_ref).strip()

        if worktree.exists():
            current_branch = self._run(
                "-C", str(worktree), "branch", "--show-current"
            ).strip()
            if current_branch != branch:
                raise GitOperationError(
                    f"worktree {worktree} is on {current_branch!r}, expected {branch!r}"
                )
            return PreparedWorkspace(checkout, worktree, base_sha, branch)

        worktree.parent.mkdir(parents=True, exist_ok=True)
        local_branch = self._run(
            "-C", str(checkout), "branch", "--list", branch
        ).strip()
        if local_branch:
            self._run("-C", str(checkout), "worktree", "add", str(worktree), branch)
        else:
            self._run(
                "-C",
                str(checkout),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                base_sha,
            )
        return PreparedWorkspace(checkout, worktree, base_sha, branch)

    def commit_and_push(
        self,
        project: ProjectConfig,
        workspace: PreparedWorkspace,
        *,
        message: str,
    ) -> str:
        worktree = str(workspace.worktree_path)
        self._run("-C", worktree, "add", "--all")
        staged = subprocess.run(
            ["git", "-C", worktree, "diff", "--cached", "--quiet"],
            check=False,
        ).returncode
        if staged == 1:
            self._run(
                "-C",
                worktree,
                "-c",
                "user.name=Enzo",
                "-c",
                "user.email=enzo@localhost",
                "commit",
                "-m",
                message,
            )
        elif staged != 0:
            raise GitOperationError("git diff --cached failed")
        head_sha = self._run("-C", worktree, "rev-parse", "HEAD").strip()
        if project.auto_push:
            self._run(
                "-C",
                worktree,
                "push",
                "--set-upstream",
                "origin",
                "--",
                workspace.branch,
            )
        return head_sha

    def diff_stat(self, workspace: PreparedWorkspace) -> str:
        return self._run(
            "-C", str(workspace.worktree_path), "diff", "--stat", workspace.base_sha
        )

    def diff_patch(self, workspace: PreparedWorkspace) -> str:
        return self._run(
            "-C",
            str(workspace.worktree_path),
            "diff",
            "--binary",
            workspace.base_sha,
            "HEAD",
        )

    def remote_head(self, project: ProjectConfig, *, branch: str) -> str | None:
        checkout = self.repository_root / _safe_component(project.id) / "checkout"
        output = self._run(
            "-C",
            str(checkout),
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ).strip()
        return output.split(maxsplit=1)[0] if output else None

    def cleanup(
        self,
        project: ProjectConfig,
        *,
        worktree_path: Path,
        force: bool = False,
    ) -> None:
        project_key = _safe_component(project.id)
        checkout = self.repository_root / project_key / "checkout"
        managed_root = (self.worktree_root / project_key).resolve()
        resolved_worktree = worktree_path.resolve()
        if resolved_worktree == managed_root or not resolved_worktree.is_relative_to(
            managed_root
        ):
            raise GitOperationError("refusing to clean a path outside the managed worktree root")
        lock_path = self.repository_root / project_key / ".worktree.lock"
        with _exclusive_lock(lock_path):
            if resolved_worktree.exists():
                arguments = ["-C", str(checkout), "worktree", "remove"]
                if force:
                    arguments.append("--force")
                arguments.append(str(resolved_worktree))
                self._run(*arguments)
            self._run("-C", str(checkout), "worktree", "prune")

    @staticmethod
    def _run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = _redact_credentials((completed.stderr or completed.stdout).strip())
            operation = args[0] if args else "operation"
            raise GitOperationError(f"git {operation} failed: {detail}")
        return completed.stdout


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    return cleaned[:80] or "project"


def _branch_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    return cleaned[:80] or "feature"


_CREDENTIAL_URL = re.compile(r"(?P<scheme>https?://)[^\s/@]+(?::[^\s/@]*)?@")


def _redact_credentials(value: str) -> str:
    return _CREDENTIAL_URL.sub(r"\g<scheme>***@", value)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    with path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

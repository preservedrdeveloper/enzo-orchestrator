from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from conftest import Harness, make_harness
from test_plan_cycle import create_ready_plan

from enzo.container import Container
from enzo.domain import AgentRole, EventOutcome
from enzo.execution.coordinator import ExecutionCommandError, RemoteHeadMismatchError
from enzo.execution.models import CommandSpec, ProjectConfig
from enzo.web import create_app


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _remote_repository(root: Path) -> Path:
    source = root / "source"
    remote = root / "remote.git"
    source.mkdir(parents=True)
    _git("init", "-b", "main", cwd=source)
    (source / "README.md").write_text("# Fixture repository\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "commit",
        "-m",
        "initial",
        cwd=source,
    )
    _git("init", "--bare", str(remote))
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "--set-upstream", "origin", "main", cwd=source)
    return remote


def _project(remote: Path, *, failing_lint: bool = False) -> ProjectConfig:
    success_check = (
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('enzo-implementation.txt').is_file()",
    )
    lint = (sys.executable, "-c", "raise SystemExit(7)") if failing_lint else success_check
    return ProjectConfig(
        id="repository-project",
        external_id="plane-project",
        name="Repository Project",
        repository_url=str(remote),
        commands=(
            CommandSpec("lint", lint),
            CommandSpec("test", success_check),
            CommandSpec("build", success_check),
        ),
    )


def _approve_plan(harness: Harness, external_id: str) -> None:
    command = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve plan@1",
        f"{external_id}:approve-plan",
    )
    assert harness.orchestrator.ingest_comment(command).outcome is EventOutcome.APPLIED


def test_repository_bound_execution_creates_worktree_evidence_commit_and_push(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-EXEC-1")
    _approve_plan(harness, external_id)

    harness.orchestrator.drain(max_steps=50)

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    execution = snapshot["execution"]
    assert snapshot["stage"] == "VERIFICATION"
    assert execution["status"] == "SUCCEEDED"
    assert execution["agent"] == "fake"
    assert execution["result"]["agent_runs"][0]["runner"] == {
        "name": "fake",
        "kind": "fake",
        "provider": None,
        "model": None,
    }
    assert execution["implementation_revisions"][0]["status"] == "REVIEW"
    assert execution["branch"] == "ai/plane-exec-1"
    assert execution["base_sha"] != execution["head_sha"]
    assert Path(execution["worktree_path"], "enzo-implementation.txt").is_file()
    assert [item["name"] for item in execution["evidence"]] == ["lint", "test", "build"]
    assert {item["status"] for item in execution["evidence"]} == {"PASS"}
    assert _git("--git-dir", str(remote), "rev-parse", execution["branch"]) == execution[
        "head_sha"
    ]
    coder = next(request for request in harness.agent.requests if request.role is AgentRole.CODER)
    assert coder.workspace_path == execution["worktree_path"]
    assert coder.metadata["execution_plan"]["tasks"]
    assert "# Specification" in coder.metadata["approved_spec"]
    assert "# Implementation Plan" in coder.metadata["approved_plan"]
    assert any(
        message.kind == "IMPLEMENTATION_REVIEW_REQUIRED"
        for message in harness.plane.notifications
    )

    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
        project=harness.project,
        execution_coordinator=harness.execution_coordinator,
    )

    async def view_evidence() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/executions/{execution['id']}")
            assert response.status_code == 200
            assert "Implementation · PLANE-EXEC-1" in response.text
            assert "lint: PASS" in response.text
            assert "Diff summary" in response.text

    asyncio.run(view_evidence())


def test_two_workers_cannot_prepare_duplicate_execution_or_worktree(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-EXEC-RACE")
    _approve_plan(harness, external_id)
    barrier = threading.Barrier(3)
    results: list[str | None] = []

    def work(worker_id: str) -> None:
        barrier.wait()
        results.append(harness.orchestrator.run_one_job(worker_id=worker_id))

    workers = [
        threading.Thread(target=work, args=("worker-a",)),
        threading.Thread(target=work, args=("worker-b",)),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)

    assert sum(result is not None for result in results) == 1
    with harness.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        execution = connection.execute("SELECT * FROM executions").fetchone()
    assert Path(execution["worktree_path"]).is_dir()
    harness.orchestrator.drain(max_steps=50)
    assert harness.orchestrator.feature_snapshot(external_id)["execution"]["status"] == "SUCCEEDED"


def test_failed_verification_preserves_worktree_and_does_not_push(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, failing_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-EXEC-FAIL")
    _approve_plan(harness, external_id)

    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)

    execution = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert execution["status"] == "FAILED"
    assert Path(execution["worktree_path"]).is_dir()
    assert execution["head_sha"] is None
    assert [(item["name"], item["status"]) for item in execution["evidence"]] == [
        ("lint", "FAIL")
    ]
    branches = _git("--git-dir", str(remote), "branch", "--list", execution["branch"])
    assert branches == ""


def test_relative_data_root_still_records_an_absolute_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _remote_repository(tmp_path / "git")
    monkeypatch.chdir(tmp_path)
    harness = make_harness(Path("relative-enzo"), project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-RELATIVE")
    _approve_plan(harness, external_id)

    harness.orchestrator.drain(max_steps=50)

    worktree = Path(
        harness.orchestrator.feature_snapshot(external_id)["execution"]["worktree_path"]
    )
    assert worktree.is_absolute()
    assert worktree.is_dir()
    assert worktree.is_relative_to(tmp_path / "relative-enzo" / "worktrees")


def test_implementation_feedback_reuses_worktree_and_approval_closes_execution(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-IMPL-REVIEW")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    first = harness.orchestrator.feature_snapshot(external_id)
    execution_id = first["execution"]["id"]
    worktree = Path(first["execution"]["worktree_path"])
    first_head = first["execution"]["head_sha"]

    changes = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo request-changes implementation@1\n"
        "- [Behavior] Record the reviewed edge case.\n"
        "- [Tests] Keep the deterministic checks passing.",
        "implementation:changes:1",
    )
    assert harness.orchestrator.ingest_comment(changes).outcome is EventOutcome.APPLIED
    changed = harness.orchestrator.feature_snapshot(external_id)
    assert changed["stage"] == "IMPLEMENTATION"
    assert changed["execution"]["implementation_revisions"][0]["status"] == (
        "CHANGES_REQUESTED"
    )

    address = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo address-with-agent implementation@1",
        "implementation:address:1",
    )
    assert harness.orchestrator.ingest_comment(address).outcome is EventOutcome.APPLIED
    harness.orchestrator.drain(max_steps=50)

    revised = harness.orchestrator.feature_snapshot(external_id)
    execution = revised["execution"]
    assert revised["stage"] == "VERIFICATION"
    assert execution["id"] == execution_id
    assert Path(execution["worktree_path"]) == worktree
    assert execution["head_sha"] != first_head
    assert [item["status"] for item in execution["implementation_revisions"]] == [
        "CHANGES_REQUESTED",
        "REVIEW",
    ]
    with harness.database.read() as connection:
        resolved = connection.execute(
            """SELECT status, resolution_type, resolved_in_revision_id
               FROM implementation_feedback_items ORDER BY source_ordinal"""
        ).fetchall()
        assert [(item["status"], item["resolution_type"]) for item in resolved] == [
            ("RESOLVED", "AGENT"),
            ("RESOLVED", "AGENT"),
        ]
        assert len({item["resolved_in_revision_id"] for item in resolved}) == 1
        assert connection.execute(
            "SELECT count(*) FROM verification_evidence WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()[0] == 6

    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve implementation@2",
        "implementation:approve:2",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED
    assert harness.orchestrator.feature_snapshot(external_id)["stage"] == "VERIFICATION"
    harness.orchestrator.drain(max_steps=50)

    completed = harness.orchestrator.feature_snapshot(external_id)
    assert completed["stage"] == "DONE"
    assert completed["execution"]["status"] == "CLOSED"
    assert completed["execution"]["implementation_revisions"][-1]["status"] == "APPROVED"
    assert not worktree.exists()
    assert _git(
        "--git-dir",
        str(remote),
        "rev-parse",
        completed["execution"]["branch"],
    ) == completed["execution"]["head_sha"]
    assert harness.plane.stages[external_id] == "DONE"


def test_approval_refuses_cleanup_when_remote_head_no_longer_matches(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-REMOTE-RACE")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    before = harness.orchestrator.feature_snapshot(external_id)
    execution = before["execution"]
    worktree = Path(execution["worktree_path"])
    _git(
        "--git-dir",
        str(remote),
        "update-ref",
        f"refs/heads/{execution['branch']}",
        execution["base_sha"],
    )

    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve implementation@1",
        "remote-race:approve",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED
    with pytest.raises(RemoteHeadMismatchError):
        harness.orchestrator.drain(max_steps=50)

    after = harness.orchestrator.feature_snapshot(external_id)
    assert after["stage"] == "VERIFICATION"
    assert after["execution"]["status"] == "SUCCEEDED"
    assert worktree.exists()
    with harness.database.read() as connection:
        close = connection.execute(
            "SELECT status FROM jobs WHERE kind = 'CLOSE_IMPLEMENTATION'"
        ).fetchone()
        assert close["status"] == "FAILED"


def test_implementation_review_decision_race_has_one_winner(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-IMPL-RACE")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve implementation@1",
        "implementation-race:approve",
    )
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes implementation@1\n- [Tests] Add another assertion.",
        "implementation-race:changes",
    )
    barrier = threading.Barrier(3)
    results = []

    def submit(comment) -> None:  # noqa: ANN001
        barrier.wait()
        results.append(harness.orchestrator.ingest_comment(comment))

    workers = [
        threading.Thread(target=submit, args=(approve,)),
        threading.Thread(target=submit, args=(changes,)),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)

    assert sorted(result.outcome for result in results) == sorted(
        [EventOutcome.APPLIED, EventOutcome.STALE]
    )
    with harness.database.read() as connection:
        decisions = connection.execute(
            """SELECT count(*) FROM implementation_reviews
               WHERE status IN ('APPROVED', 'CHANGES_REQUESTED')"""
        ).fetchone()[0]
        assert decisions == 1

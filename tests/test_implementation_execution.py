from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import Harness, make_harness
from test_plan_cycle import create_ready_plan

from enzo.container import Container
from enzo.domain import AgentRole, EventOutcome, ExecutionRecoveryAction
from enzo.execution.coordinator import (
    ExecutionCommandError,
    RemoteHeadMismatchError,
    upsert_project,
)
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


def _project(
    remote: Path,
    *,
    failing_lint: bool = False,
    fail_once_lint: bool = False,
    fail_once_bootstrap: bool = False,
) -> ProjectConfig:
    success_check = (
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('enzo-implementation.txt').is_file()",
    )
    fail_once = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "marker=Path('.enzo-fail-once'); exists=marker.exists(); "
        "marker.write_text('attempted'); raise SystemExit(0 if exists else 7)",
    )
    lint = (
        (sys.executable, "-c", "raise SystemExit(7)")
        if failing_lint
        else fail_once if fail_once_lint else success_check
    )
    bootstrap = (CommandSpec("bootstrap", fail_once),) if fail_once_bootstrap else ()
    return ProjectConfig(
        id="repository-project",
        external_id="plane-project",
        name="Repository Project",
        repository_url=str(remote),
        commands=bootstrap + (
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


def _container(harness: Harness) -> Container:
    return Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent_runner,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
        project=harness.project,
        execution_coordinator=harness.execution_coordinator,
    )


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
            assert "Implementation review" in response.text
            assert "Changed files" in response.text
            assert "/review-assets/implementation.js" in response.text
            detail = (await client.get(f"/api/executions/{execution['id']}")).json()
            assert detail["external_id"] == "PLANE-EXEC-1"
            assert detail["feature_stage"] == "VERIFICATION"
            assert detail["review_write_enabled"] is True
            assert detail["worktree_present"] is True
            assert "worktree_path" not in detail
            assert [(item["name"], item["status"]) for item in detail["evidence"]] == [
                ("lint", "PASS"),
                ("test", "PASS"),
                ("build", "PASS"),
            ]
            assert all("log_path" not in item for item in detail["evidence"])
            assert "diff_path" not in detail["implementation_revisions"][0]
            assert detail["implementation_revisions"][0]["diff"]

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


def test_failed_verification_retries_same_execution_and_worktree(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, fail_once_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-EXEC-RETRY")
    _approve_plan(harness, external_id)

    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)
    failed = harness.orchestrator.feature_snapshot(external_id)["execution"]
    execution_id = failed["id"]
    worktree = Path(failed["worktree_path"])
    revision_id = failed["current_revision_id"]
    recovery = harness.execution_coordinator.recovery_snapshot(execution_id)
    assert recovery == {
        "operation": "VERIFY_IMPLEMENTATION",
        "status": "FAILED",
        "attempts": 1,
        "error": failed["error"],
        "can_retry": True,
        "can_abandon": True,
    }

    result = harness.orchestrator.submit_execution_recovery(
        execution_id=execution_id,
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.RETRY,
        delivery_id="execution-retry:verification",
    )
    duplicate = harness.orchestrator.submit_execution_recovery(
        execution_id=execution_id,
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.RETRY,
        delivery_id="execution-retry:verification",
    )
    assert result.outcome is EventOutcome.APPLIED
    assert duplicate.outcome is EventOutcome.DUPLICATE
    queued = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert queued["id"] == execution_id
    assert queued["worktree_path"] == str(worktree)
    assert queued["status"] == "RUNNING"
    assert queued["implementation_revisions"][0]["id"] == revision_id
    assert queued["implementation_revisions"][0]["status"] == "UPDATING"

    harness.orchestrator.drain(max_steps=50)

    recovered = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert recovered["id"] == execution_id
    assert recovered["worktree_path"] == str(worktree)
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["implementation_revisions"][0]["id"] == revision_id
    assert recovered["implementation_revisions"][0]["status"] == "REVIEW"
    assert [(item["name"], item["status"]) for item in recovered["evidence"]] == [
        ("lint", "PASS"),
        ("test", "PASS"),
        ("build", "PASS"),
    ]
    with harness.database.read() as connection:
        verify = connection.execute(
            "SELECT status, attempts, last_error FROM jobs WHERE kind = 'VERIFY_IMPLEMENTATION'"
        ).fetchone()
    assert (verify["status"], verify["attempts"]) == ("SUCCEEDED", 2)
    assert "lint failed" in verify["last_error"]


def test_each_failed_retry_attempt_gets_a_distinct_outbox_notice(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, failing_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-RETRY-FAILS-AGAIN")
    _approve_plan(harness, external_id)
    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)
    execution = harness.orchestrator.feature_snapshot(external_id)["execution"]

    retried = harness.orchestrator.submit_execution_recovery(
        execution_id=execution["id"],
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.RETRY,
        delivery_id="execution-retry:fails-again",
    )
    assert retried.outcome is EventOutcome.APPLIED
    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)

    with harness.database.read() as connection:
        failures = connection.execute(
            """SELECT idempotency_key, payload_json FROM outbox_messages
               WHERE kind = 'IMPLEMENTATION_FAILED' ORDER BY created_at"""
        ).fetchall()
    assert [item["idempotency_key"].rsplit(":", 2)[-2] for item in failures] == [
        "1",
        "2",
    ]


def test_bootstrap_failure_records_worktree_and_can_retry_prepare(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, fail_once_bootstrap=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-BOOTSTRAP-RETRY")
    _approve_plan(harness, external_id)

    with pytest.raises(ExecutionCommandError, match="bootstrap failed"):
        harness.orchestrator.drain(max_steps=50)
    failed = harness.orchestrator.feature_snapshot(external_id)["execution"]
    worktree = Path(failed["worktree_path"])
    assert failed["status"] == "FAILED"
    assert failed["current_revision_id"] is None
    assert worktree.is_dir()
    assert [(item["name"], item["status"]) for item in failed["evidence"]] == [
        ("bootstrap", "FAIL")
    ]

    result = harness.orchestrator.submit_execution_recovery(
        execution_id=failed["id"],
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.RETRY,
        delivery_id="execution-retry:bootstrap",
    )
    assert result.outcome is EventOutcome.APPLIED
    harness.orchestrator.drain(max_steps=50)

    recovered = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert recovered["id"] == failed["id"]
    assert recovered["worktree_path"] == str(worktree)
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["implementation_revisions"][0]["status"] == "REVIEW"
    assert [(item["name"], item["status"]) for item in recovered["evidence"]] == [
        ("bootstrap", "PASS"),
        ("lint", "PASS"),
        ("test", "PASS"),
        ("build", "PASS"),
    ]


def test_abandon_cancels_failed_execution_and_force_cleans_dirty_worktree(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, failing_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-EXEC-ABANDON")
    _approve_plan(harness, external_id)
    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)
    failed = harness.orchestrator.feature_snapshot(external_id)["execution"]
    worktree = Path(failed["worktree_path"])
    assert worktree.is_dir()
    assert (worktree / "enzo-implementation.txt").is_file()

    result = harness.orchestrator.submit_execution_recovery(
        execution_id=failed["id"],
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.ABANDON,
        delivery_id="execution-abandon:1",
    )
    duplicate = harness.orchestrator.submit_execution_recovery(
        execution_id=failed["id"],
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.ABANDON,
        delivery_id="execution-abandon:1",
    )
    assert result.outcome is EventOutcome.APPLIED
    assert duplicate.outcome is EventOutcome.DUPLICATE
    cancelled = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert cancelled["status"] == "CANCELLED"
    assert worktree.is_dir()

    harness.orchestrator.drain(max_steps=50)

    abandoned = harness.orchestrator.feature_snapshot(external_id)["execution"]
    assert abandoned["status"] == "CANCELLED"
    assert abandoned["worktree_path"] is None
    assert not worktree.exists()
    assert abandoned["result"]["recovery"]["abandoned"] is True
    assert abandoned["result"]["recovery"]["worktree_cleaned"] is True
    assert [(item["name"], item["status"]) for item in abandoned["evidence"]] == [
        ("lint", "FAIL")
    ]
    with harness.database.read() as connection:
        jobs = connection.execute(
            "SELECT kind, status FROM jobs WHERE kind LIKE '%IMPLEMENTATION'"
        ).fetchall()
    assert ("VERIFY_IMPLEMENTATION", "CANCELLED") in {
        (job["kind"], job["status"]) for job in jobs
    }
    assert ("CLEANUP_ABANDONED_IMPLEMENTATION", "SUCCEEDED") in {
        (job["kind"], job["status"]) for job in jobs
    }
    assert any(
        message.kind == "IMPLEMENTATION_ABANDONED"
        for message in harness.plane.notifications
    )


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
            "SELECT status, attempts FROM jobs WHERE kind = 'CLOSE_IMPLEMENTATION'"
        ).fetchone()
        assert close["status"] == "FAILED"
        assert close["attempts"] == 1
    recovery = harness.execution_coordinator.recovery_snapshot(execution["id"])
    assert recovery["operation"] == "CLOSE_IMPLEMENTATION"
    assert recovery["can_retry"] is True
    assert recovery["can_abandon"] is False

    _git(
        "--git-dir",
        str(remote),
        "update-ref",
        f"refs/heads/{execution['branch']}",
        execution["head_sha"],
    )
    retried = harness.orchestrator.submit_execution_recovery(
        execution_id=execution["id"],
        actor_id="reviewer-1",
        action=ExecutionRecoveryAction.RETRY,
        delivery_id="execution-retry:close",
    )
    assert retried.outcome is EventOutcome.APPLIED
    harness.orchestrator.drain(max_steps=50)
    completed = harness.orchestrator.feature_snapshot(external_id)
    assert completed["stage"] == "DONE"
    assert completed["execution"]["status"] == "CLOSED"
    assert not worktree.exists()
    with harness.database.read() as connection:
        close = connection.execute(
            "SELECT status, attempts FROM jobs WHERE kind = 'CLOSE_IMPLEMENTATION'"
        ).fetchone()
    assert (close["status"], close["attempts"]) == ("SUCCEEDED", 2)


def test_retry_and_abandon_race_has_one_recovery_winner(tmp_path: Path) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, fail_once_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-RECOVERY-RACE")
    _approve_plan(harness, external_id)
    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)
    execution = harness.orchestrator.feature_snapshot(external_id)["execution"]

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(_container(harness)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            retry, abandon = await asyncio.gather(
                client.post(
                    f"/api/executions/{execution['id']}/recovery-actions",
                    json={"action": "RETRY", "request_id": "recovery-race:retry"},
                ),
                client.post(
                    f"/api/executions/{execution['id']}/recovery-actions",
                    json={"action": "ABANDON", "request_id": "recovery-race:abandon"},
                ),
            )
            assert sorted([retry.status_code, abandon.status_code]) == [200, 409]

    asyncio.run(scenario())
    with harness.database.read() as connection:
        events = connection.execute(
            """SELECT event_type FROM domain_events
               WHERE event_type IN ('EXECUTION_RETRY_QUEUED', 'EXECUTION_ABANDONED')"""
        ).fetchall()
    assert len(events) == 1

    winner = events[0]["event_type"]
    harness.orchestrator.drain(max_steps=50)
    final = harness.orchestrator.feature_snapshot(external_id)["execution"]
    if winner == "EXECUTION_RETRY_QUEUED":
        assert final["status"] == "SUCCEEDED"
        assert final["implementation_revisions"][0]["status"] == "REVIEW"
    else:
        assert final["status"] == "CANCELLED"
        assert final["worktree_path"] is None


def test_review_surface_does_not_expose_or_recover_another_projects_execution(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(
        tmp_path / "enzo",
        project=_project(remote, failing_lint=True),
    )
    external_id = create_ready_plan(harness, external_id="PLANE-OTHER-PROJECT")
    _approve_plan(harness, external_id)
    with pytest.raises(ExecutionCommandError, match="lint failed"):
        harness.orchestrator.drain(max_steps=50)
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    execution_id = snapshot["execution"]["id"]
    other_project = replace(
        harness.project,
        id="other-project",
        external_id="other-plane-project",
    )
    upsert_project(harness.database, other_project)
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE features SET project_id = ? WHERE id = ?",
            (other_project.id, snapshot["id"]),
        )
        connection.execute(
            "UPDATE executions SET project_id = ? WHERE id = ?",
            (other_project.id, execution_id),
        )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(_container(harness)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/executions/{execution_id}")
            page = await client.get(f"/executions/{execution_id}")
            recovery = await client.post(
                f"/api/executions/{execution_id}/recovery-actions",
                json={"action": "ABANDON", "request_id": "other-project:abandon"},
            )
            assert detail.status_code == 404
            assert page.status_code == 404
            assert recovery.status_code == 422

    asyncio.run(scenario())


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


def test_implementation_review_surface_drives_feedback_revision_cycle(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-IMPL-WEB")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    first = harness.orchestrator.feature_snapshot(external_id)["execution"]
    execution_id = first["id"]
    revision_id = first["current_revision_id"]
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent_runner,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
        project=harness.project,
        execution_coordinator=harness.execution_coordinator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = {
                "action": "REQUEST_CHANGES",
                "request_id": "implementation-web:changes",
                "feedback": [
                    {
                        "section": "enzo-implementation.txt",
                        "comment": "Record the observable reviewed behavior.",
                    },
                    {
                        "section": "Tests",
                        "comment": "Keep all deterministic checks passing.",
                    },
                ],
            }
            blank = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json={
                    "action": "REQUEST_CHANGES",
                    "request_id": "implementation-web:blank",
                    "feedback": [{"section": "Tests", "comment": "   \n"}],
                },
            )
            assert blank.status_code == 422
            changed = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json=request,
            )
            assert changed.status_code == 200
            assert changed.json()["event"]["outcome"] == "APPLIED"

            duplicate = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json=request,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["event"]["outcome"] == "DUPLICATE"

            detail = (await client.get(f"/api/executions/{execution_id}")).json()
            first_revision = detail["implementation_revisions"][0]
            assert first_revision["status"] == "CHANGES_REQUESTED"
            assert [item["section"] for item in first_revision["feedback"]] == [
                "enzo-implementation.txt",
                "Tests",
            ]

            addressed = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json={
                    "action": "ADDRESS_WITH_AGENT",
                    "request_id": "implementation-web:address",
                },
            )
            assert addressed.status_code == 200
            updating_id = addressed.json()["execution"]["current_revision_id"]
            assert updating_id != revision_id
            assert "worktree_path" not in addressed.json()["execution"]
            assert addressed.json()["execution"]["implementation_revisions"][-1][
                "status"
            ] == "UPDATING"

            assert (await client.post("/worker/drain")).status_code == 200
            revised = (await client.get(f"/api/executions/{execution_id}")).json()
            current = revised["implementation_revisions"][-1]
            assert current["id"] == updating_id
            assert current["status"] == "REVIEW"
            assert [item["resolution_type"] for item in current["resolved_feedback"]] == [
                "AGENT",
                "AGENT",
            ]
            assert {item["implementation_revision_id"] for item in current["resolved_feedback"]} == {
                revision_id
            }
            evidence = [
                item
                for item in revised["evidence"]
                if item["implementation_revision_id"] == updating_id
            ]
            assert [(item["name"], item["status"]) for item in evidence] == [
                ("lint", "PASS"),
                ("test", "PASS"),
                ("build", "PASS"),
            ]
            stale = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json={"action": "APPROVE", "request_id": "implementation-web:stale"},
            )
            assert stale.status_code == 409

    asyncio.run(scenario())


def test_implementation_review_surface_decision_race_has_one_winner(
    tmp_path: Path,
) -> None:
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-IMPL-WEB-RACE")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    execution = harness.orchestrator.feature_snapshot(external_id)["execution"]
    revision_id = execution["current_revision_id"]
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent_runner,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
        project=harness.project,
        execution_coordinator=harness.execution_coordinator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            approve, changes = await asyncio.gather(
                client.post(
                    f"/api/implementation-revisions/{revision_id}/review-actions",
                    json={"action": "APPROVE", "request_id": "web-race:approve"},
                ),
                client.post(
                    f"/api/implementation-revisions/{revision_id}/review-actions",
                    json={
                        "action": "REQUEST_CHANGES",
                        "request_id": "web-race:changes",
                        "feedback": [{"section": "Tests", "comment": "Add an assertion."}],
                    },
                ),
            )
            assert sorted([approve.status_code, changes.status_code]) == [200, 409]
            detail = (await client.get(f"/api/executions/{execution['id']}")).json()
            assert detail["implementation_revisions"][0]["status"] in {
                "APPROVED",
                "CHANGES_REQUESTED",
            }

    asyncio.run(scenario())


def test_plane_mode_implementation_review_surface_is_read_only_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENZO_REVIEW_UI_WRITE_ENABLED", raising=False)
    remote = _remote_repository(tmp_path / "git")
    harness = make_harness(tmp_path / "enzo", project=_project(remote))
    external_id = create_ready_plan(harness, external_id="PLANE-IMPL-READONLY")
    _approve_plan(harness, external_id)
    harness.orchestrator.drain(max_steps=50)
    execution = harness.orchestrator.feature_snapshot(external_id)["execution"]
    revision_id = execution["current_revision_id"]
    container = replace(
        Container(
            database=harness.database,
            artifact_store=harness.artifact_store,
            agent_runner=harness.agent_runner,
            task_manager=harness.plane,
            orchestrator=harness.orchestrator,
            project=harness.project,
            execution_coordinator=harness.execution_coordinator,
        ),
        task_manager=object(),
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/executions/{execution['id']}")
            assert detail.status_code == 200
            assert detail.json()["review_write_enabled"] is False
            approve = await client.post(
                f"/api/implementation-revisions/{revision_id}/review-actions",
                json={"action": "APPROVE", "request_id": "readonly:implementation"},
            )
            assert approve.status_code == 403
            recover = await client.post(
                f"/api/executions/{execution['id']}/recovery-actions",
                json={"action": "RETRY", "request_id": "readonly:recovery"},
            )
            assert recover.status_code == 403
            unchanged = (await client.get(f"/api/executions/{execution['id']}")).json()
            assert unchanged["implementation_revisions"][0]["status"] == "REVIEW"

    asyncio.run(scenario())

from __future__ import annotations

import threading
from pathlib import Path

from conftest import Harness
from test_spec_cycle import create_ready_spec

from enzo.domain import AgentRole, ArtifactType, EventOutcome


def create_ready_plan(harness: Harness, *, external_id: str = "PLANE-PLAN") -> str:
    create_ready_spec(harness, external_id=external_id)
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve spec@1",
        f"{external_id}:approve-spec",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot is not None
    assert snapshot["stage"] == "PLAN"
    assert snapshot["artifact"]["type"] == "PLAN"
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert snapshot["execution_plan"]["schema_version"] == 1
    assert [task["key"] for task in snapshot["execution_plan"]["tasks"]] == [
        "inspect-context",
        "implement-feature",
        "verify-feature",
    ]
    revision = harness.orchestrator.revision_detail(
        snapshot["artifact"]["current_revision_id"]
    )
    assert "## Verification Mapping" in revision["content"]
    yaml_path = Path(revision["manifest_path"]).with_name("execution-plan.yaml")
    assert yaml_path.exists()
    assert "depends_on:" in yaml_path.read_text(encoding="utf-8")
    request = harness.agent.requests[-1]
    assert request.role is AgentRole.PLAN
    assert "spec.md" in request.metadata["previous_artifacts"]
    with harness.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM execution_plans").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM execution_tasks").fetchone()[0] == 3
    return external_id


def test_plan_uses_same_review_cycle_and_stops_at_implementation_boundary(
    harness: Harness,
) -> None:
    external_id = create_ready_plan(harness)
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes plan@1\n"
        "- [Execution Breakdown] Keep verification after implementation.\n"
        "- [File-Level Changes] Resolve exact paths during repository inspection.",
        "plan:changes:1",
    )
    assert harness.orchestrator.ingest_comment(changes).outcome is EventOutcome.APPLIED
    assert harness.orchestrator.feature_snapshot(external_id)["unresolved_feedback"] == 2

    address = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo address-with-agent plan@1",
        "plan:address:1",
    )
    assert harness.orchestrator.ingest_comment(address).outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["revision_no"] == 2
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert snapshot["unresolved_feedback"] == 0
    assert len(snapshot["execution_plan"]["tasks"]) == 3
    request = harness.agent.requests[-1]
    assert request.role is AgentRole.FEEDBACK_RESOLVER
    assert request.metadata["artifact_type"] is ArtifactType.PLAN
    assert request.metadata["current_execution_plan"]["schema_version"] == 1

    approve = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo approve plan@2",
        "plan:approve:2",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["stage"] == "IMPLEMENTATION"
    assert snapshot["artifact"]["type"] == "PLAN"
    assert snapshot["artifact"]["artifact_status"] == "APPROVED"
    history = harness.orchestrator.artifact_history(external_id, ArtifactType.PLAN)
    assert [(row["revision_no"], row["status"]) for row in history] == [
        (1, "CHANGES_REQUESTED"),
        (2, "APPROVED"),
    ]
    with harness.database.read() as connection:
        boundary = connection.execute(
            "SELECT status FROM jobs WHERE kind = 'PREPARE_IMPLEMENTATION'"
        ).fetchone()
        assert boundary is not None
        assert boundary["status"] == "PENDING"


def test_plan_review_decision_race_has_one_winner(harness: Harness) -> None:
    external_id = create_ready_plan(harness, external_id="PLANE-PLAN-RACE")
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve plan@1",
        "plan-race:approve",
    )
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes plan@1\n- [Testing Plan] Add an integration assertion.",
        "plan-race:changes",
    )
    barrier = threading.Barrier(3)
    results = []

    def submit(comment) -> None:  # noqa: ANN001
        barrier.wait()
        results.append(harness.orchestrator.ingest_comment(comment))

    threads = [
        threading.Thread(target=submit, args=(approve,)),
        threading.Thread(target=submit, args=(changes,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(result.outcome for result in results) == sorted(
        [EventOutcome.APPLIED, EventOutcome.STALE]
    )
    with harness.database.read() as connection:
        decided = connection.execute(
            """SELECT count(*) FROM reviews r
               JOIN artifact_revisions ar ON ar.id = r.artifact_revision_id
               JOIN artifacts a ON a.id = ar.artifact_id
               WHERE a.type = 'PLAN' AND r.status != 'PENDING'"""
        ).fetchone()[0]
        assert decided == 1

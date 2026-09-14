from __future__ import annotations

import threading
from pathlib import Path

from conftest import Harness
from test_intent_cycle import create_ready_intent

from enzo.domain import AgentRole, ArtifactType, EventOutcome


def create_ready_spec(harness: Harness, *, external_id: str = "PLANE-SPEC") -> str:
    create_ready_intent(harness, external_id=external_id)
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve intent@1",
        f"{external_id}:approve-intent",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot is not None
    assert snapshot["stage"] == "SPEC"
    assert snapshot["artifact"]["type"] == "SPEC"
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert "## Verification Contract" in Path(snapshot["artifact"]["content_path"]).read_text(
        encoding="utf-8"
    )
    request = harness.agent.requests[-1]
    assert request.role is AgentRole.SPEC
    assert "intent.md" in request.metadata["previous_artifacts"]
    return external_id


def test_spec_uses_same_feedback_revision_and_approval_cycle(harness: Harness) -> None:
    external_id = create_ready_spec(harness)
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes spec@1\n"
        "- [User Experience] Use the existing mobile drawer.\n"
        "- [Verification Contract] Require a refresh-persistence E2E assertion.",
        "spec:changes:1",
    )
    assert harness.orchestrator.ingest_comment(changes).outcome is EventOutcome.APPLIED
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["artifact_status"] == "CHANGES_REQUESTED"
    assert snapshot["unresolved_feedback"] == 2

    address = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo address-with-agent spec@1",
        "spec:address:1",
    )
    assert harness.orchestrator.ingest_comment(address).outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["revision_no"] == 2
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert snapshot["unresolved_feedback"] == 0
    request = harness.agent.requests[-1]
    assert request.role is AgentRole.FEEDBACK_RESOLVER
    assert request.metadata["artifact_type"] == ArtifactType.SPEC
    assert "intent.md" in request.metadata["previous_artifacts"]
    assert [item.section for item in request.unresolved_feedback] == [
        "User Experience",
        "Verification Contract",
    ]

    approve = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo approve spec@2",
        "spec:approve:2",
    )
    assert harness.orchestrator.ingest_comment(approve).outcome is EventOutcome.APPLIED

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["stage"] == "PLAN"
    assert snapshot["artifact"]["type"] == "PLAN"
    assert snapshot["artifact"]["artifact_status"] == "GENERATING"
    artifacts = {row["type"]: row for row in snapshot["artifacts"]}
    assert artifacts["SPEC"]["artifact_status"] == "APPROVED"
    history = harness.orchestrator.artifact_history(external_id, ArtifactType.SPEC)
    assert [(row["revision_no"], row["status"]) for row in history] == [
        (1, "CHANGES_REQUESTED"),
        (2, "APPROVED"),
    ]
    with harness.database.read() as connection:
        plan_job = connection.execute(
            "SELECT status, payload_json FROM jobs WHERE kind = 'GENERATE_PLAN'"
        ).fetchone()
        assert plan_job is not None
        assert plan_job["status"] == "PENDING"
        assert "revision_id" in plan_job["payload_json"]


def test_spec_review_decision_race_has_one_winner(harness: Harness) -> None:
    external_id = create_ready_spec(harness, external_id="PLANE-SPEC-RACE")
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve spec@1",
        "spec-race:approve",
    )
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes spec@1\n- [Verification Contract] Add an E2E assertion.",
        "spec-race:changes",
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
               WHERE a.type = 'SPEC' AND r.status != 'PENDING'"""
        ).fetchone()[0]
        assert decided == 1

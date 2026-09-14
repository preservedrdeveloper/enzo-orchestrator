from __future__ import annotations

from pathlib import Path

from conftest import Harness

from enzo.domain import EventOutcome


def create_ready_intent(harness: Harness, *, external_id: str = "PLANE-1") -> str:
    feature = harness.plane.create_feature("Carrier filter", external_id)
    result = harness.orchestrator.ingest_feature(feature, delivery_id=f"create:{external_id}")
    assert result.outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot is not None
    assert snapshot["stage"] == "INTENT"
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    return external_id


def test_agent_feedback_revision_and_approval(harness: Harness) -> None:
    external_id = create_ready_intent(harness)

    changes = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo request-changes intent@1\n"
        "- [UX] Reuse the mobile filter drawer.\n"
        "- [Scope] Explicitly exclude saved searches.",
        "comment:changes:1",
    )
    result = harness.orchestrator.ingest_comment(changes)
    assert result.outcome is EventOutcome.APPLIED
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["artifact_status"] == "CHANGES_REQUESTED"
    assert snapshot["unresolved_feedback"] == 2

    address = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo address-with-agent intent@1",
        "comment:address:1",
    )
    result = harness.orchestrator.ingest_comment(address)
    assert result.outcome is EventOutcome.APPLIED
    harness.orchestrator.drain()

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["revision_no"] == 2
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert snapshot["unresolved_feedback"] == 0
    request = harness.agent.requests[-1]
    assert [item.comment for item in request.unresolved_feedback] == [
        "Reuse the mobile filter drawer.",
        "Explicitly exclude saved searches.",
    ]
    assert "Carrier filter" in (request.current_content or "")

    approve = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo approve intent@2",
        "comment:approve:2",
    )
    result = harness.orchestrator.ingest_comment(approve)
    assert result.outcome is EventOutcome.APPLIED
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["stage"] == "SPEC"
    assert snapshot["artifact"]["type"] == "SPEC"
    assert snapshot["artifact"]["artifact_status"] == "GENERATING"
    artifacts = {row["type"]: row for row in snapshot["artifacts"]}
    assert artifacts["INTENT"]["artifact_status"] == "APPROVED"

    history = harness.orchestrator.artifact_history(external_id)
    assert [(row["revision_no"], row["status"]) for row in history] == [
        (1, "CHANGES_REQUESTED"),
        (2, "APPROVED"),
    ]
    assert Path(history[0]["content_path"]).read_text(encoding="utf-8") != Path(
        history[1]["content_path"]
    ).read_text(encoding="utf-8")
    with harness.database.read() as connection:
        spec_job = connection.execute(
            "SELECT * FROM jobs WHERE kind = 'GENERATE_SPEC'"
        ).fetchone()
        assert spec_job is not None
        assert spec_job["status"] == "PENDING"
        assert "revision_id" in spec_job["payload_json"]


def test_manual_edit_creates_new_revision_and_resolves_feedback(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-MANUAL")
    comment = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo request-changes intent@1\n- [Scope] Add an explicit boundary.",
        "comment:manual:changes",
    )
    harness.orchestrator.ingest_comment(comment)
    original = harness.orchestrator.revision_detail(
        harness.orchestrator.artifact_history(external_id)[0]["id"]
    )
    edited = (original["content"] or "") + "\nManual clarification.\n"

    result = harness.orchestrator.submit_manual_revision(
        external_feature_id=external_id,
        actor_id="reviewer-1",
        base_revision=1,
        content=edited,
        delivery_id="manual-edit:1",
    )

    assert result.outcome is EventOutcome.APPLIED
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["artifact"]["revision_no"] == 2
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    assert snapshot["unresolved_feedback"] == 0
    with harness.database.read() as connection:
        feedback = connection.execute("SELECT * FROM feedback_items").fetchone()
        assert feedback["status"] == "RESOLVED"
        assert feedback["resolution_type"] == "MANUAL"


def test_non_reviewer_cannot_cross_gate(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-AUTH")
    comment = harness.plane.add_comment(
        external_id,
        "not-a-reviewer",
        "@enzo approve intent@1",
        "comment:unauthorized",
    )

    result = harness.orchestrator.ingest_comment(comment)

    assert result.outcome is EventOutcome.REJECTED
    assert harness.orchestrator.feature_snapshot(external_id)["stage"] == "INTENT"


def test_request_changes_requires_first_class_items(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-NO-FEEDBACK")
    comment = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo request-changes intent@1\nPlease make it better.",
        "comment:no-items",
    )

    result = harness.orchestrator.ingest_comment(comment)

    assert result.outcome is EventOutcome.REJECTED
    assert harness.orchestrator.feature_snapshot(external_id)["artifact"]["artifact_status"] == "REVIEW"

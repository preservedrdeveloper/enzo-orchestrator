from __future__ import annotations

import threading
import time
from pathlib import Path

from conftest import Harness, make_harness
from test_intent_cycle import create_ready_intent

from enzo.domain import CommandAction, EventOutcome, FeedbackDraft
from enzo.fakes import BlockingFakeAgent


def test_duplicate_feature_delivery_creates_one_job_and_one_revision(harness: Harness) -> None:
    feature = harness.plane.create_feature("Duplicate delivery", "PLANE-DUP")
    first = harness.orchestrator.ingest_feature(feature, delivery_id="delivery:duplicate")
    second = harness.orchestrator.ingest_feature(feature, delivery_id="delivery:duplicate")
    harness.orchestrator.drain()

    assert first.outcome is EventOutcome.APPLIED
    assert second.outcome is EventOutcome.DUPLICATE
    assert harness.agent.call_count == 1
    with harness.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM features").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifact_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM jobs WHERE kind='GENERATE_INTENT'").fetchone()[0] == 1


def test_simultaneous_approve_and_request_changes_has_one_winner(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-RACE-DECISION")
    approve = harness.plane.add_comment(
        external_id, "reviewer-1", "@enzo approve intent@1", "race:approve"
    )
    changes = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo request-changes intent@1\n- [Scope] Define the boundary.",
        "race:changes",
    )
    barrier = threading.Barrier(3)
    results = []

    def submit(comment) -> None:
        barrier.wait()
        results.append(harness.orchestrator.ingest_comment(comment))

    threads = [threading.Thread(target=submit, args=(approve,)), threading.Thread(target=submit, args=(changes,))]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(result.outcome for result in results) == sorted(
        [EventOutcome.APPLIED, EventOutcome.STALE]
    )
    with harness.database.read() as connection:
        review = connection.execute("SELECT status FROM reviews").fetchone()["status"]
        assert review in {"APPROVED", "CHANGES_REQUESTED"}
        assert connection.execute(
            "SELECT count(*) FROM reviews WHERE status != 'PENDING'"
        ).fetchone()[0] == 1


def test_review_surface_decisions_share_the_same_race_fence(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-RACE-REVIEW-UI")
    revision_id = harness.orchestrator.feature_snapshot(external_id)["artifact"][
        "current_revision_id"
    ]
    barrier = threading.Barrier(3)
    results = []

    def submit(action: CommandAction, delivery_id: str) -> None:
        barrier.wait()
        results.append(
            harness.orchestrator.submit_artifact_review(
                revision_id=revision_id,
                actor_id="reviewer-1",
                action=action,
                feedback=(FeedbackDraft("Scope", "Define the boundary."),)
                if action is CommandAction.REQUEST_CHANGES
                else (),
                delivery_id=delivery_id,
            )
        )

    threads = [
        threading.Thread(
            target=submit, args=(CommandAction.APPROVE, "review-ui:race:approve")
        ),
        threading.Thread(
            target=submit,
            args=(CommandAction.REQUEST_CHANGES, "review-ui:race:changes"),
        ),
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
        assert connection.execute(
            "SELECT count(*) FROM reviews WHERE status != 'PENDING'"
        ).fetchone()[0] == 1


def test_two_workers_cannot_claim_same_job(harness: Harness) -> None:
    feature = harness.plane.create_feature("Worker claim", "PLANE-RACE-WORKER")
    harness.orchestrator.ingest_feature(feature, delivery_id="worker:create")
    barrier = threading.Barrier(3)
    claims = []

    def claim(worker_id: str) -> None:
        barrier.wait()
        claims.append(harness.orchestrator.claim_next_job(worker_id=worker_id))

    threads = [threading.Thread(target=claim, args=("worker-a",)), threading.Thread(target=claim, args=("worker-b",))]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    harness.orchestrator.execute_claimed_job(claimed[0])
    assert harness.agent.call_count == 1


def test_expired_lease_is_reclaimed_without_duplicate_agent_application(harness: Harness) -> None:
    feature = harness.plane.create_feature("Lease recovery", "PLANE-LEASE")
    harness.orchestrator.ingest_feature(feature, delivery_id="lease:create")
    abandoned = harness.orchestrator.claim_next_job(worker_id="dead-worker", lease_seconds=0.01)
    assert abandoned is not None
    time.sleep(0.02)
    reclaimed = harness.orchestrator.claim_next_job(worker_id="live-worker", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.id == abandoned.id
    assert reclaimed.attempts == 2

    harness.orchestrator.execute_claimed_job(abandoned)
    harness.orchestrator.execute_claimed_job(reclaimed)

    assert harness.agent.call_count == 1
    assert harness.orchestrator.feature_snapshot("PLANE-LEASE")["artifact"]["artifact_status"] == "REVIEW"


def test_agent_result_is_superseded_when_expected_feature_version_changes(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    agent = BlockingFakeAgent(entered, release)
    harness = make_harness(tmp_path, agent=agent)
    feature = harness.plane.create_feature("Stale agent", "PLANE-STALE-AGENT")
    harness.orchestrator.ingest_feature(feature, delivery_id="stale-agent:create")
    claim = harness.orchestrator.claim_next_job(worker_id="blocking-worker")
    assert claim is not None

    thread = threading.Thread(target=harness.orchestrator.execute_claimed_job, args=(claim,))
    thread.start()
    assert entered.wait(timeout=5)
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE features SET version = version + 1 WHERE external_id = 'PLANE-STALE-AGENT'"
        )
    release.set()
    thread.join(timeout=5)

    with harness.database.read() as connection:
        assert connection.execute("SELECT status FROM agent_runs").fetchone()["status"] == "SUPERSEDED"
        assert connection.execute("SELECT status FROM jobs WHERE id = ?", (claim.id,)).fetchone()["status"] == "CANCELLED"
        revision = connection.execute("SELECT status, content_path FROM artifact_revisions").fetchone()
        assert revision["status"] == "GENERATING"
        assert revision["content_path"] is None


def test_outbox_retries_after_commit_without_duplicate_plane_message(harness: Harness) -> None:
    feature = harness.plane.create_feature("Outbox retry", "PLANE-OUTBOX")
    harness.orchestrator.ingest_feature(feature, delivery_id="outbox:create")
    harness.plane.fail_next_deliveries = 1

    first_id = harness.orchestrator.run_one_outbox(worker_id="outbox-worker")
    second_id = harness.orchestrator.run_one_outbox(worker_id="outbox-worker")

    assert first_id == second_id
    assert len(harness.plane.deliveries) == 1
    with harness.database.read() as connection:
        row = connection.execute("SELECT status, attempts FROM outbox_messages").fetchone()
        assert row["status"] == "SENT"
        assert row["attempts"] == 2


def test_stale_revision_approval_is_rejected(harness: Harness) -> None:
    external_id = create_ready_intent(harness, external_id="PLANE-STALE-REV")
    request = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo request-changes intent@1\n- [Problem] Clarify the actor.",
        "stale-rev:changes",
    )
    harness.orchestrator.ingest_comment(request)
    address = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo address-with-agent intent@1",
        "stale-rev:address",
    )
    harness.orchestrator.ingest_comment(address)
    harness.orchestrator.drain()
    stale = harness.plane.add_comment(
        external_id,
        "reviewer-2",
        "@enzo approve intent@1",
        "stale-rev:approve-old",
    )

    result = harness.orchestrator.ingest_comment(stale)

    assert result.outcome is EventOutcome.STALE
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["stage"] == "INTENT"
    assert snapshot["artifact"]["revision_no"] == 2
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"

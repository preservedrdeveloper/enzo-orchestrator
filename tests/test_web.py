import asyncio
from dataclasses import replace

import httpx
from conftest import Harness
from test_plan_cycle import create_ready_plan

from enzo.container import Container
from enzo.web import create_app


def test_fake_plane_api_and_artifact_viewer(harness: Harness) -> None:
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
    )
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/fake/features",
                json={"title": "Carrier filter", "external_id": "PLANE-WEB"},
            )
            assert created.status_code == 200
            assert created.json()["event"]["outcome"] == "APPLIED"
            assert (await client.post("/worker/drain")).json()["steps"] >= 1

            snapshot = await client.get("/api/features/PLANE-WEB")
            assert snapshot.status_code == 200
            payload = snapshot.json()
            assert payload["stage"] == "INTENT"
            assert payload["artifact"]["artifact_status"] == "REVIEW"

            revision_id = payload["artifact"]["current_revision_id"]
            viewer = await client.get(f"/artifacts/{revision_id}")
            assert viewer.status_code == 200
            assert "Enzo Review" in viewer.text
            assert "/review-assets/app.js" in viewer.text
            detail = await client.get(f"/api/artifact-revisions/{revision_id}")
            assert detail.status_code == 200
            assert detail.json()["display_name"] == "intent.md"
            assert "Carrier filter" in detail.json()["content"]

    asyncio.run(scenario())


def test_plan_artifact_viewer_includes_machine_readable_execution_plan(
    harness: Harness,
) -> None:
    external_id = create_ready_plan(harness, external_id="PLANE-WEB-PLAN")
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            viewer = await client.get(
                f"/artifacts/{snapshot['artifact']['current_revision_id']}"
            )
            assert viewer.status_code == 200
            detail = await client.get(
                f"/api/artifact-revisions/{snapshot['artifact']['current_revision_id']}"
            )
            assert detail.status_code == 200
            assert detail.json()["display_name"] == "plan.md"
            assert "inspect-context" in detail.json()["execution_plan_content"]

    asyncio.run(scenario())


def test_review_surface_selection_feedback_and_agent_revision(harness: Harness) -> None:
    external_id = "PLANE-WEB-REVIEW"
    feature = harness.plane.create_feature("Carrier filter", external_id)
    harness.orchestrator.ingest_feature(feature, delivery_id="web-review:create")
    harness.orchestrator.drain()
    snapshot = harness.orchestrator.feature_snapshot(external_id)
    revision_id = snapshot["artifact"]["current_revision_id"]
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = (await client.get(f"/api/artifact-revisions/{revision_id}")).json()
            content = detail["content"]
            exact = "Clarify the requested product behavior."
            start = content.index(exact)
            end = start + len(exact)
            feedback = {
                "section": "Scope",
                "comment": "Name the observable behavior explicitly.",
                "selection": {
                    "exact": exact,
                    "start_offset": start,
                    "end_offset": end,
                    "prefix": content[max(0, start - 40):start],
                    "suffix": content[end:end + 40],
                },
            }
            request = {
                "action": "REQUEST_CHANGES",
                "feedback": [feedback],
                "request_id": "selection-feedback-1",
            }
            changed = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions", json=request
            )
            assert changed.status_code == 200
            assert changed.json()["event"]["outcome"] == "APPLIED"

            duplicate = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions", json=request
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["event"]["outcome"] == "DUPLICATE"

            reviewed = (await client.get(f"/api/artifact-revisions/{revision_id}")).json()
            assert reviewed["status"] == "CHANGES_REQUESTED"
            assert reviewed["feedback"][0]["location"]["exact"] == exact

            address = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions",
                json={"action": "ADDRESS_WITH_AGENT", "request_id": "address-selection-1"},
            )
            assert address.status_code == 200
            next_revision_id = address.json()["feature"]["artifact"]["current_revision_id"]
            assert next_revision_id != revision_id
            assert (await client.post("/worker/drain")).status_code == 200
            revised = (await client.get(f"/api/artifact-revisions/{next_revision_id}")).json()
            assert revised["status"] == "REVIEW"

    asyncio.run(scenario())


def test_review_surface_rejects_a_selection_from_different_content(harness: Harness) -> None:
    feature = harness.plane.create_feature("Anchored review", "PLANE-BAD-ANCHOR")
    harness.orchestrator.ingest_feature(feature, delivery_id="bad-anchor:create")
    harness.orchestrator.drain()
    revision_id = harness.orchestrator.feature_snapshot("PLANE-BAD-ANCHOR")["artifact"][
        "current_revision_id"
    ]
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions",
                json={
                    "action": "REQUEST_CHANGES",
                    "request_id": "bad-anchor:review",
                    "feedback": [{
                        "section": "Scope",
                        "comment": "This anchor is stale.",
                        "selection": {
                            "exact": "not present",
                            "start_offset": 0,
                            "end_offset": 11,
                        },
                    }],
                },
            )
            assert response.status_code == 422
            detail = (await client.get(f"/api/artifact-revisions/{revision_id}")).json()
            assert detail["status"] == "REVIEW"
            assert detail["feedback"] == []

    asyncio.run(scenario())


def test_review_surface_manual_edit_creates_an_immutable_child(harness: Harness) -> None:
    feature = harness.plane.create_feature("Manual review", "PLANE-WEB-MANUAL")
    harness.orchestrator.ingest_feature(feature, delivery_id="web-manual:create")
    harness.orchestrator.drain()
    snapshot = harness.orchestrator.feature_snapshot("PLANE-WEB-MANUAL")
    revision_id = snapshot["artifact"]["current_revision_id"]
    original = harness.orchestrator.revision_detail(revision_id)["content"]
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            changed = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions",
                json={
                    "action": "REQUEST_CHANGES",
                    "request_id": "web-manual:changes",
                    "feedback": [{
                        "section": "Constraints",
                        "comment": "Record a manual constraint.",
                    }],
                },
            )
            assert changed.status_code == 200
            saved = await client.post(
                f"/api/artifact-revisions/{revision_id}/manual-revisions",
                json={
                    "content": original + "\nManual constraint recorded.\n",
                    "request_id": "web-manual:save",
                },
            )
            assert saved.status_code == 200
            next_revision_id = saved.json()["feature"]["artifact"]["current_revision_id"]
            assert next_revision_id != revision_id
            revised = (await client.get(f"/api/artifact-revisions/{next_revision_id}")).json()
            assert revised["status"] == "REVIEW"
            assert revised["content"].endswith("Manual constraint recorded.\n")
            old = (await client.get(f"/api/artifact-revisions/{revision_id}")).json()
            assert old["content"] == original
            assert old["feedback"][0]["resolution_type"] == "MANUAL"

    asyncio.run(scenario())


def test_plane_mode_disables_unauthenticated_review_writes_and_admin_api(
    harness: Harness, monkeypatch
) -> None:
    monkeypatch.delenv("ENZO_REVIEW_UI_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("ENZO_ADMIN_API_ENABLED", raising=False)
    feature = harness.plane.create_feature("Read-only review", "PLANE-READONLY")
    harness.orchestrator.ingest_feature(feature, delivery_id="readonly:create")
    harness.orchestrator.drain()
    revision_id = harness.orchestrator.feature_snapshot("PLANE-READONLY")["artifact"][
        "current_revision_id"
    ]
    container = replace(
        Container(
            database=harness.database,
            artifact_store=harness.artifact_store,
            agent_runner=harness.agent,
            task_manager=harness.plane,
            orchestrator=harness.orchestrator,
        ),
        task_manager=object(),
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/artifact-revisions/{revision_id}")
            assert detail.status_code == 200
            assert detail.json()["review_write_enabled"] is False
            review = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions",
                json={"action": "APPROVE", "request_id": "readonly:approve"},
            )
            assert review.status_code == 403
            assert (await client.post("/worker/drain")).status_code == 404
            assert harness.orchestrator.feature_snapshot("PLANE-READONLY")["stage"] == "INTENT"

    asyncio.run(scenario())


def test_plane_mode_write_opt_in_requires_an_explicit_reviewer_identity(
    harness: Harness, monkeypatch
) -> None:
    monkeypatch.setenv("ENZO_REVIEW_UI_WRITE_ENABLED", "true")
    monkeypatch.delenv("ENZO_REVIEW_UI_ACTOR_ID", raising=False)
    feature = harness.plane.create_feature("Attributed review", "PLANE-ATTRIBUTION")
    harness.orchestrator.ingest_feature(feature, delivery_id="attribution:create")
    harness.orchestrator.drain()
    revision_id = harness.orchestrator.feature_snapshot("PLANE-ATTRIBUTION")["artifact"][
        "current_revision_id"
    ]
    container = replace(
        Container(
            database=harness.database,
            artifact_store=harness.artifact_store,
            agent_runner=harness.agent,
            task_manager=harness.plane,
            orchestrator=harness.orchestrator,
        ),
        task_manager=object(),
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/artifact-revisions/{revision_id}/review-actions",
                json={"action": "APPROVE", "request_id": "attribution:approve"},
            )
            assert response.status_code == 503
            assert harness.orchestrator.feature_snapshot("PLANE-ATTRIBUTION")["stage"] == "INTENT"

    asyncio.run(scenario())

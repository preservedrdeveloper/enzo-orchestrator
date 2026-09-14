from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
from conftest import Harness

from enzo.container import Container
from enzo.plane import PlaneProjectBinding, PlaneWebhookHandler, plane_comment_text
from enzo.web import create_app

SECRET = "plane-webhook-secret"


def binding() -> PlaneProjectBinding:
    return PlaneProjectBinding(
        base_url="http://plane.test",
        api_token="test-token",
        webhook_secret=SECRET,
        workspace_slug="enzo-lab",
        project_id="project-1",
        artifact_base_url="http://enzo.test",
        managed_label="enzo:managed",
    )


def signed(payload: dict, *, delivery_id: str) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    return raw, {
        "Content-Type": "application/json",
        "X-Plane-Delivery": delivery_id,
        "X-Plane-Event": payload["event"],
        "X-Plane-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
    }


def issue_payload() -> dict:
    return {
        "event": "issue",
        "action": "created",
        "webhook_id": "webhook-1",
        "workspace_id": "workspace-1",
        "workspace_slug": "enzo-lab",
        "data": {
            "id": "work-item-1",
            "name": "Carrier filter",
            "project": "project-1",
            "labels": [{"id": "label-1", "name": "enzo:managed"}],
        },
        "activity": None,
    }


def app_for(harness: Harness):  # noqa: ANN201
    container = Container(
        database=harness.database,
        artifact_store=harness.artifact_store,
        agent_runner=harness.agent,
        task_manager=harness.plane,
        orchestrator=harness.orchestrator,
        plane_webhook=PlaneWebhookHandler(binding()),
    )
    return create_app(container)


def test_plane_comment_html_preserves_command_and_feedback_lines() -> None:
    assert plane_comment_text(
        "<p>@enzo request-changes intent@1</p>"
        "<ul><li><p>[Problem] Name the affected actor.</p></li>"
        "<li><p>Clarify success.</p></li></ul>"
    ) == (
        "@enzo request-changes intent@1\n"
        "- [Problem] Name the affected actor.\n"
        "- Clarify success."
    )


def test_signed_plane_webhook_drives_real_ingress_and_deduplicates(harness: Harness) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app_for(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://enzo.test") as client:
            raw, headers = signed(issue_payload(), delivery_id="delivery-1")
            first = await client.post("/webhooks/plane", content=raw, headers=headers)
            duplicate = await client.post("/webhooks/plane", content=raw, headers=headers)

            assert first.status_code == 202
            assert first.json()["event"]["outcome"] == "APPLIED"
            assert duplicate.status_code == 202
            assert duplicate.json()["event"]["outcome"] == "DUPLICATE"
            assert (await client.post("/worker/drain")).json()["steps"] >= 1
            snapshot = (await client.get("/api/features/work-item-1")).json()
            assert snapshot["stage"] == "INTENT"
            assert snapshot["artifact"]["artifact_status"] == "REVIEW"

    asyncio.run(scenario())


def test_plane_feedback_comment_creates_first_class_feedback(harness: Harness) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app_for(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://enzo.test") as client:
            raw, headers = signed(issue_payload(), delivery_id="delivery-feature")
            await client.post("/webhooks/plane", content=raw, headers=headers)
            await client.post("/worker/drain")

            comment = {
                "event": "issue_comment",
                "action": "created",
                "webhook_id": "webhook-1",
                "workspace_id": "workspace-1",
                "workspace_slug": "enzo-lab",
                "data": {
                    "id": "comment-1",
                    "issue": "work-item-1",
                    "project": "project-1",
                    "actor": "reviewer-1",
                    "comment_html": (
                        "<p>@enzo request-changes intent@1</p>"
                        "<ul><li><p>[Problem] Name the affected actor.</p></li>"
                        "<li><p>[Scope] Exclude admin reporting.</p></li></ul>"
                    ),
                },
                "activity": None,
            }
            raw, headers = signed(comment, delivery_id="delivery-comment")
            response = await client.post("/webhooks/plane", content=raw, headers=headers)

            assert response.status_code == 202
            assert response.json()["event"]["outcome"] == "APPLIED"
            snapshot = (await client.get("/api/features/work-item-1")).json()
            assert snapshot["artifact"]["artifact_status"] == "CHANGES_REQUESTED"
            assert snapshot["unresolved_feedback"] == 2

            address = {
                **comment,
                "data": {
                    **comment["data"],
                    "id": "comment-2",
                    "comment_html": "<p>@enzo address-with-agent intent@1</p>",
                },
            }
            raw, headers = signed(address, delivery_id="delivery-address")
            addressed = await client.post("/webhooks/plane", content=raw, headers=headers)
            assert addressed.json()["event"]["outcome"] == "APPLIED"
            await client.post("/worker/drain")
            snapshot = (await client.get("/api/features/work-item-1")).json()
            assert snapshot["artifact"]["revision_no"] == 2
            assert snapshot["artifact"]["artifact_status"] == "REVIEW"
            assert snapshot["unresolved_feedback"] == 0

            approve = {
                **comment,
                "data": {
                    **comment["data"],
                    "id": "comment-3",
                    "comment_html": "<p>@enzo approve intent@2</p>",
                },
            }
            raw, headers = signed(approve, delivery_id="delivery-approve")
            approved = await client.post("/webhooks/plane", content=raw, headers=headers)
            assert approved.json()["event"]["outcome"] == "APPLIED"
            snapshot = (await client.get("/api/features/work-item-1")).json()
            assert snapshot["stage"] == "SPEC"
            assert snapshot["artifact"]["type"] == "SPEC"
            assert snapshot["artifact"]["artifact_status"] == "GENERATING"
            await client.post("/worker/drain")
            snapshot = (await client.get("/api/features/work-item-1")).json()
            assert snapshot["artifact"]["artifact_status"] == "REVIEW"
            with harness.database.read() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM jobs WHERE kind = 'GENERATE_SPEC' AND status = 'SUCCEEDED'"
                ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_invalid_plane_signature_is_rejected_before_state_change(harness: Harness) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app_for(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://enzo.test") as client:
            raw, headers = signed(issue_payload(), delivery_id="delivery-bad-signature")
            headers["X-Plane-Signature"] = "0" * 64
            response = await client.post("/webhooks/plane", content=raw, headers=headers)

            assert response.status_code == 401
            assert harness.orchestrator.feature_snapshot("work-item-1") is None

    asyncio.run(scenario())


def test_unmanaged_work_item_is_ignored(harness: Harness) -> None:
    async def scenario() -> None:
        payload = issue_payload()
        payload["data"]["labels"] = []
        raw, headers = signed(payload, delivery_id="delivery-unmanaged")
        transport = httpx.ASGITransport(app=app_for(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://enzo.test") as client:
            response = await client.post("/webhooks/plane", content=raw, headers=headers)

            assert response.status_code == 202
            assert response.json()["outcome"] == "IGNORED"
            assert harness.orchestrator.feature_snapshot("work-item-1") is None

    asyncio.run(scenario())

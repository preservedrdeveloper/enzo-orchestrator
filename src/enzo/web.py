from __future__ import annotations

import os
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import environment_flag
from .container import Container, build_container
from .domain import EventResult
from .fakes import FakeTaskManagerAdapter
from .plane import PlaneWebhookError, PlaneWebhookSignatureError
from .review_surface import REVIEW_UI_DIR, build_review_router


class CreateFeatureBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    external_id: str | None = None
    delivery_id: str | None = None


class CommentBody(BaseModel):
    actor_id: str = "reviewer-1"
    body: str = Field(min_length=1)
    comment_id: str | None = None


class ManualRevisionBody(BaseModel):
    actor_id: str = "reviewer-1"
    base_revision: int = Field(gt=0)
    content: str = Field(min_length=1)
    execution_plan: dict | None = None
    delivery_id: str | None = None


def _result(result: EventResult) -> dict[str, str | None]:
    return {
        "outcome": result.outcome,
        "message": result.message,
        "feature_id": result.feature_id,
    }


def create_app(container: Container | None = None) -> FastAPI:
    services = container or build_container()
    application = FastAPI(title="Enzo V0", version="0.1.0")
    application.state.container = services
    admin_api_enabled = environment_flag(
        os.environ,
        "ENZO_ADMIN_API_ENABLED",
        default=isinstance(services.task_manager, FakeTaskManagerAdapter),
    )
    application.mount(
        "/review-assets",
        StaticFiles(directory=REVIEW_UI_DIR),
        name="review-assets",
    )
    application.include_router(build_review_router(services))

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "task_manager": "plane" if services.plane_webhook else "fake",
        }

    @application.post("/webhooks/plane", status_code=202)
    async def plane_webhook(request: Request) -> dict[str, object]:
        if services.plane_webhook is None:
            raise HTTPException(status_code=404, detail="Plane integration is not configured")
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Plane webhook body is too large")
        try:
            event = services.plane_webhook.parse(raw_body=raw_body, headers=request.headers)
        except PlaneWebhookSignatureError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except PlaneWebhookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if event.ignored_reason:
            return {
                "delivery_id": event.delivery_id,
                "outcome": "IGNORED",
                "message": event.ignored_reason,
            }
        if event.feature:
            result = services.orchestrator.ingest_feature(
                event.feature,
                delivery_id=event.delivery_id,
            )
        elif event.comment:
            result = services.orchestrator.ingest_comment(event.comment)
        else:  # defensive: the normalizer must choose one domain event or ignore it
            raise HTTPException(status_code=400, detail="Plane webhook has no domain event")
        return {"delivery_id": event.delivery_id, "event": _result(result)}

    def fake_task_manager() -> FakeTaskManagerAdapter:
        if not isinstance(services.task_manager, FakeTaskManagerAdapter):
            raise HTTPException(status_code=404, detail="Fake task-manager API is disabled")
        return services.task_manager

    @application.post("/fake/features")
    def create_feature(body: CreateFeatureBody) -> dict[str, object]:
        feature = fake_task_manager().create_feature(body.title, body.external_id)
        result = services.orchestrator.ingest_feature(
            feature,
            delivery_id=body.delivery_id or f"feature-created:{feature.id}",
        )
        return {"feature": feature.__dict__, "event": _result(result)}

    @application.post("/fake/features/{external_feature_id}/comments")
    def add_comment(external_feature_id: str, body: CommentBody) -> dict[str, object]:
        task_manager = fake_task_manager()
        if external_feature_id not in task_manager.features:
            raise HTTPException(status_code=404, detail="FakePlane feature not found")
        comment = task_manager.add_comment(
            external_feature_id,
            body.actor_id,
            body.body,
            body.comment_id,
        )
        return {"comment": comment.__dict__, "event": _result(services.orchestrator.ingest_comment(comment))}

    @application.post("/fake/features/{external_feature_id}/manual-revisions")
    def manual_revision(external_feature_id: str, body: ManualRevisionBody) -> dict[str, object]:
        result = services.orchestrator.submit_manual_revision(
            external_feature_id=external_feature_id,
            actor_id=body.actor_id,
            base_revision=body.base_revision,
            content=body.content,
            delivery_id=body.delivery_id or f"manual-edit:{uuid.uuid4()}",
            execution_plan=body.execution_plan,
        )
        return {"event": _result(result)}

    @application.post("/worker/drain")
    def drain() -> dict[str, int]:
        if not admin_api_enabled:
            raise HTTPException(status_code=404, detail="Admin API is disabled")
        return {"steps": services.orchestrator.drain()}

    @application.get("/api/features/{external_feature_id}")
    def feature(external_feature_id: str) -> dict[str, object]:
        snapshot = services.orchestrator.feature_snapshot(external_feature_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Feature not found")
        artifact_type = snapshot["artifact"]["type"] if snapshot.get("artifact") else "INTENT"
        snapshot["history"] = services.orchestrator.artifact_history(
            external_feature_id, artifact_type
        )
        return snapshot

    return application


app = create_app()


def main() -> None:
    uvicorn.run(
        "enzo.web:app",
        host=os.environ.get("ENZO_HOST", "127.0.0.1"),
        port=int(os.environ.get("ENZO_PORT", "8000")),
        reload=False,
    )

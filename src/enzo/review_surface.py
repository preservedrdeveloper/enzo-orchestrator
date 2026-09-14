from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import environment_flag
from .container import Container
from .domain import (
    ArtifactType,
    CommandAction,
    EventOutcome,
    EventResult,
    FeedbackDraft,
    TextSelection,
)
from .fakes import FakeTaskManagerAdapter

REVIEW_UI_DIR = Path(__file__).with_name("review_ui")


class TextSelectionBody(BaseModel):
    exact: str = Field(min_length=1, max_length=10_000)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    prefix: str = Field(default="", max_length=80)
    suffix: str = Field(default="", max_length=80)


class FeedbackItemBody(BaseModel):
    section: str | None = Field(default=None, max_length=200)
    comment: str = Field(min_length=1, max_length=10_000)
    selection: TextSelectionBody | None = None


class ReviewActionBody(BaseModel):
    action: Literal["APPROVE", "REQUEST_CHANGES", "ADDRESS_WITH_AGENT"]
    feedback: list[FeedbackItemBody] = Field(default_factory=list, max_length=100)
    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=200
    )


class ReviewManualRevisionBody(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    execution_plan: dict | None = None
    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=200
    )


def _result(result: EventResult) -> dict[str, str | None]:
    return {
        "outcome": result.outcome,
        "message": result.message,
        "feature_id": result.feature_id,
    }


def build_review_router(services: Container) -> APIRouter:
    router = APIRouter()
    fake_mode = isinstance(services.task_manager, FakeTaskManagerAdapter)
    write_enabled = environment_flag(
        os.environ,
        "ENZO_REVIEW_UI_WRITE_ENABLED",
        default=fake_mode,
    )

    def review_actor_id() -> str:
        configured = os.environ.get("ENZO_REVIEW_UI_ACTOR_ID", "").strip()
        actor_id = configured or next(iter(sorted(services.orchestrator.reviewer_ids)), "")
        if not actor_id or actor_id not in services.orchestrator.reviewer_ids:
            raise HTTPException(
                status_code=503,
                detail="ENZO_REVIEW_UI_ACTOR_ID must name a configured Enzo reviewer",
            )
        return actor_id

    def write_actor_id() -> str:
        if not write_enabled:
            raise HTTPException(
                status_code=403,
                detail=(
                    "review mutations are disabled; enable them only on a trusted "
                    "deployment with ENZO_REVIEW_UI_WRITE_ENABLED=true"
                ),
            )
        if not fake_mode and not os.environ.get("ENZO_REVIEW_UI_ACTOR_ID", "").strip():
            raise HTTPException(
                status_code=503,
                detail="Plane mode review writes require ENZO_REVIEW_UI_ACTOR_ID",
            )
        return review_actor_id()

    @router.get("/api/artifact-revisions/{revision_id}")
    def artifact_revision(revision_id: str) -> dict[str, object]:
        detail = services.orchestrator.revision_detail(revision_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Artifact revision not found")
        detail["review_actor_id"] = review_actor_id()
        detail["review_write_enabled"] = write_enabled
        detail["history"] = services.orchestrator.artifact_history(
            detail["external_id"], detail["artifact_type"]
        )
        return detail

    @router.post("/api/artifact-revisions/{revision_id}/review-actions")
    def artifact_review_action(
        revision_id: str, body: ReviewActionBody
    ) -> dict[str, object]:
        feedback = tuple(
            FeedbackDraft(
                section=item.section.strip() if item.section and item.section.strip() else None,
                comment=item.comment.strip(),
                selection=(
                    TextSelection(**item.selection.model_dump()) if item.selection else None
                ),
            )
            for item in body.feedback
        )
        result = services.orchestrator.submit_artifact_review(
            revision_id=revision_id,
            actor_id=write_actor_id(),
            action=CommandAction(body.action),
            feedback=feedback,
            delivery_id=f"review-ui:{body.request_id}",
        )
        if result.outcome is EventOutcome.REJECTED:
            raise HTTPException(status_code=422, detail=result.message)
        if result.outcome is EventOutcome.STALE:
            raise HTTPException(status_code=409, detail=result.message)
        detail = services.orchestrator.revision_detail(revision_id)
        snapshot = (
            services.orchestrator.feature_snapshot(detail["external_id"])
            if detail
            else None
        )
        return {"event": _result(result), "revision": detail, "feature": snapshot}

    @router.post("/api/artifact-revisions/{revision_id}/manual-revisions")
    def artifact_manual_revision(
        revision_id: str, body: ReviewManualRevisionBody
    ) -> dict[str, object]:
        detail = services.orchestrator.revision_detail(revision_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Artifact revision not found")
        try:
            result = services.orchestrator.submit_manual_revision(
                external_feature_id=detail["external_id"],
                actor_id=write_actor_id(),
                base_revision=detail["revision_no"],
                content=body.content,
                delivery_id=f"review-ui:{body.request_id}",
                artifact_type=ArtifactType(detail["artifact_type"]),
                execution_plan=body.execution_plan,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result.outcome is EventOutcome.REJECTED:
            raise HTTPException(status_code=422, detail=result.message)
        if result.outcome is EventOutcome.STALE:
            raise HTTPException(status_code=409, detail=result.message)
        snapshot = services.orchestrator.feature_snapshot(detail["external_id"])
        return {"event": _result(result), "feature": snapshot}

    @router.get("/artifacts/{revision_id}", response_class=HTMLResponse)
    def artifact(revision_id: str) -> HTMLResponse:
        if not services.orchestrator.revision_detail(revision_id):
            raise HTTPException(status_code=404, detail="Artifact revision not found")
        return HTMLResponse((REVIEW_UI_DIR / "index.html").read_text(encoding="utf-8"))

    return router

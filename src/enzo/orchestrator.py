from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .agents import AgentRunnerError, require_run_info
from .artifacts.definitions import (
    ARTIFACT_DEFINITIONS,
    JOB_DEFINITIONS,
    ArtifactDefinition,
    validate_artifact,
)
from .artifacts.execution_plan import execution_plan_yaml, normalize_execution_plan
from .artifacts.store import ArtifactStore
from .commands import parse_review_command
from .database import Database
from .domain import (
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentWorkload,
    ArtifactStatus,
    ArtifactType,
    ClaimedJob,
    CommandAction,
    EventOutcome,
    EventResult,
    ExecutionRecoveryAction,
    ExternalComment,
    ExternalFeature,
    FeatureStage,
    FeedbackDraft,
    JobStatus,
    OutboxMessage,
    ResolutionType,
    ReviewCommand,
    ReviewTarget,
    TextSelection,
)
from .execution.coordinator import ExecutionCoordinator, upsert_project
from .execution.models import ProjectConfig
from .ports import AgentRunner, TaskManagerAdapter


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Orchestrator:
    def __init__(
        self,
        *,
        database: Database,
        artifact_store: ArtifactStore,
        agent_runner: AgentRunner,
        task_manager: TaskManagerAdapter,
        reviewer_ids: Iterable[str],
        project: ProjectConfig | None = None,
        execution_coordinator: ExecutionCoordinator | None = None,
    ) -> None:
        self.database = database
        self.artifact_store = artifact_store
        self.agent_runner = agent_runner
        self.task_manager = task_manager
        self.reviewer_ids = frozenset(reviewer_ids)
        self.project = project or ProjectConfig(
            id="default-project",
            external_id="default-project",
            name="Default Project",
        )
        upsert_project(database, self.project)
        self.execution_coordinator = execution_coordinator
        execution_jobs = (
            execution_coordinator.supported_job_kinds if execution_coordinator else ()
        )
        self.supported_job_kinds = tuple(JOB_DEFINITIONS) + tuple(execution_jobs)

    def ingest_feature(self, feature: ExternalFeature, *, delivery_id: str) -> EventResult:
        now = utc_now()
        payload = {"external_id": feature.id, "title": feature.title}
        with self.database.transaction() as connection:
            duplicate = self._begin_event(
                connection,
                delivery_id=delivery_id,
                event_type="FEATURE_CREATED",
                payload=payload,
            )
            if duplicate:
                return duplicate
            existing = connection.execute(
                "SELECT id FROM features WHERE external_id = ?", (feature.id,)
            ).fetchone()
            if existing:
                result = EventResult(EventOutcome.DUPLICATE, "feature already exists", existing["id"])
                self._finish_event(connection, delivery_id, result)
                return result

            feature_id = new_id()
            connection.execute(
                """INSERT INTO features
                   (id, external_id, title, stage, version, created_at, updated_at, project_id)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    feature_id,
                    feature.id,
                    feature.title,
                    FeatureStage.INTENT,
                    now,
                    now,
                    self.project.id,
                ),
            )
            self._create_artifact_generation(
                connection,
                feature_id=feature_id,
                definition=ARTIFACT_DEFINITIONS[ArtifactType.INTENT],
                expected_feature_version=1,
                source_revision_ids=(),
                idempotency_suffix="root",
            )
            self._insert_outbox(
                connection,
                kind="STAGE_UPDATED",
                external_feature_id=feature.id,
                idempotency_key=f"feature:{feature_id}:stage:intent:v1",
                payload={"stage": FeatureStage.INTENT, "attention": "enzo:running"},
            )
            self._domain_event(
                connection,
                feature_id=feature_id,
                event_type="FEATURE_INGESTED",
                actor_type="EXTERNAL_USER",
                actor_id="task-manager",
                subject_type="FEATURE",
                subject_id=feature_id,
                payload=payload,
            )
            result = EventResult(EventOutcome.APPLIED, "intent generation queued", feature_id)
            self._finish_event(connection, delivery_id, result)
            return result

    def ingest_comment(self, comment: ExternalComment) -> EventResult:
        command = parse_review_command(comment.body)
        payload = {
            "comment_id": comment.id,
            "external_feature_id": comment.feature_id,
            "actor_id": comment.actor_id,
            "body": comment.body,
        }
        with self.database.transaction() as connection:
            duplicate = self._begin_event(
                connection,
                delivery_id=comment.id,
                event_type="COMMENT_CREATED",
                payload=payload,
            )
            if duplicate:
                return duplicate
            if command is None:
                result = EventResult(EventOutcome.IGNORED, "normal comment; no Enzo command")
                self._finish_event(connection, comment.id, result)
                return result
            if comment.actor_id not in self.reviewer_ids:
                result = EventResult(EventOutcome.REJECTED, "actor is not an allowed reviewer")
                self._finish_event(connection, comment.id, result, rejected=True)
                return result

            if command.target is ReviewTarget.IMPLEMENTATION:
                if not self.execution_coordinator:
                    result = EventResult(
                        EventOutcome.REJECTED,
                        "implementation execution is not configured for this project",
                    )
                else:
                    result = self.execution_coordinator.handle_review_command(
                        connection,
                        comment=comment,
                        command=command,
                    )
                self._finish_event(
                    connection,
                    comment.id,
                    result,
                    rejected=result.outcome in {EventOutcome.REJECTED, EventOutcome.STALE},
                )
                return result

            result = self._handle_artifact_review_command(
                connection,
                comment=comment,
                command=command,
            )
            self._finish_event(
                connection,
                comment.id,
                result,
                rejected=result.outcome in {EventOutcome.REJECTED, EventOutcome.STALE},
            )
            return result

    def submit_artifact_review(
        self,
        *,
        revision_id: str,
        actor_id: str,
        action: CommandAction,
        feedback: tuple[FeedbackDraft, ...] = (),
        delivery_id: str,
    ) -> EventResult:
        """Apply a review-surface decision through the same artifact state machine."""

        payload = {
            "revision_id": revision_id,
            "actor_id": actor_id,
            "action": action,
            "feedback": [asdict(item) for item in feedback],
        }
        with self.database.transaction() as connection:
            duplicate = self._begin_event(
                connection,
                delivery_id=delivery_id,
                event_type="REVIEW_SURFACE_ACTION",
                payload=payload,
                source="REVIEW_SURFACE",
            )
            if duplicate:
                return duplicate
            if actor_id not in self.reviewer_ids:
                result = EventResult(EventOutcome.REJECTED, "actor is not an allowed reviewer")
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            target = connection.execute(
                """SELECT f.external_id, a.type AS artifact_type, r.revision_no
                   FROM artifact_revisions r
                   JOIN artifacts a ON a.id = r.artifact_id
                   JOIN features f ON f.id = a.feature_id
                   WHERE r.id = ?""",
                (revision_id,),
            ).fetchone()
            if not target:
                result = EventResult(EventOutcome.REJECTED, "artifact revision not found")
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            comment = ExternalComment(
                id=delivery_id,
                feature_id=target["external_id"],
                actor_id=actor_id,
                body=f"review surface: {action.value}",
            )
            command = ReviewCommand(
                action=action,
                target=ReviewTarget(target["artifact_type"]),
                revision=target["revision_no"],
                feedback=feedback,
            )
            result = self._handle_artifact_review_command(
                connection,
                comment=comment,
                command=command,
                expected_revision_id=revision_id,
            )
            self._finish_event(
                connection,
                delivery_id,
                result,
                rejected=result.outcome in {EventOutcome.REJECTED, EventOutcome.STALE},
            )
            return result

    def submit_implementation_review(
        self,
        *,
        revision_id: str,
        actor_id: str,
        action: CommandAction,
        feedback: tuple[FeedbackDraft, ...] = (),
        delivery_id: str,
    ) -> EventResult:
        """Apply a review-surface decision to an exact implementation revision."""

        payload = {
            "revision_id": revision_id,
            "actor_id": actor_id,
            "action": action,
            "feedback": [asdict(item) for item in feedback],
        }
        with self.database.transaction() as connection:
            duplicate = self._begin_event(
                connection,
                delivery_id=delivery_id,
                event_type="IMPLEMENTATION_REVIEW_SURFACE_ACTION",
                payload=payload,
                source="REVIEW_SURFACE",
            )
            if duplicate:
                return duplicate
            if actor_id not in self.reviewer_ids:
                result = EventResult(EventOutcome.REJECTED, "actor is not an allowed reviewer")
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            if not self.execution_coordinator:
                result = EventResult(
                    EventOutcome.REJECTED,
                    "implementation execution is not configured for this project",
                )
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            target = connection.execute(
                """SELECT f.external_id, ir.revision_no
                   FROM implementation_revisions ir
                   JOIN executions e ON e.id = ir.execution_id
                   JOIN features f ON f.id = e.feature_id
                   WHERE ir.id = ?""",
                (revision_id,),
            ).fetchone()
            if not target:
                result = EventResult(
                    EventOutcome.REJECTED, "implementation revision not found"
                )
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            comment = ExternalComment(
                id=delivery_id,
                feature_id=target["external_id"],
                actor_id=actor_id,
                body=f"review surface: {action.value}",
            )
            command = ReviewCommand(
                action=action,
                target=ReviewTarget.IMPLEMENTATION,
                revision=target["revision_no"],
                feedback=feedback,
            )
            result = self.execution_coordinator.handle_review_command(
                connection,
                comment=comment,
                command=command,
                expected_revision_id=revision_id,
            )
            self._finish_event(
                connection,
                delivery_id,
                result,
                rejected=result.outcome in {EventOutcome.REJECTED, EventOutcome.STALE},
            )
            return result

    def submit_execution_recovery(
        self,
        *,
        execution_id: str,
        actor_id: str,
        action: ExecutionRecoveryAction,
        delivery_id: str,
    ) -> EventResult:
        """Apply an idempotent human recovery decision to one exact execution."""

        payload = {
            "execution_id": execution_id,
            "actor_id": actor_id,
            "action": action,
        }
        with self.database.transaction() as connection:
            duplicate = self._begin_event(
                connection,
                delivery_id=delivery_id,
                event_type="EXECUTION_RECOVERY_ACTION",
                payload=payload,
                source="REVIEW_SURFACE",
            )
            if duplicate:
                return duplicate
            if actor_id not in self.reviewer_ids:
                result = EventResult(EventOutcome.REJECTED, "actor is not an allowed reviewer")
            elif not self.execution_coordinator:
                result = EventResult(
                    EventOutcome.REJECTED,
                    "implementation execution is not configured for this project",
                )
            elif action is ExecutionRecoveryAction.RETRY:
                result = self.execution_coordinator.retry_failed_execution(
                    connection,
                    execution_id=execution_id,
                    actor_id=actor_id,
                )
            else:
                result = self.execution_coordinator.abandon_failed_execution(
                    connection,
                    execution_id=execution_id,
                    actor_id=actor_id,
                )
            self._finish_event(
                connection,
                delivery_id,
                result,
                rejected=result.outcome in {EventOutcome.REJECTED, EventOutcome.STALE},
            )
            return result

    def _handle_artifact_review_command(
        self,
        connection: sqlite3.Connection,
        *,
        comment: ExternalComment,
        command: ReviewCommand,
        expected_revision_id: str | None = None,
    ) -> EventResult:
        artifact_type = command.artifact_type
        definition = ARTIFACT_DEFINITIONS[artifact_type]
        current = connection.execute(
            """SELECT f.id AS feature_id, f.external_id, f.stage, f.version,
                      a.id AS artifact_id, a.current_revision_id,
                      r.id AS revision_id, r.revision_no, r.status AS revision_status,
                      r.content_path
               FROM features f
               JOIN artifacts a ON a.feature_id = f.id AND a.type = ?
               JOIN artifact_revisions r ON r.id = a.current_revision_id
               WHERE f.external_id = ?""",
            (artifact_type, comment.feature_id),
        ).fetchone()
        if not current:
            return EventResult(
                EventOutcome.REJECTED,
                f"feature has no {definition.display_name} under Enzo review",
            )
        if (
            current["stage"] != definition.stage
            or current["revision_no"] != command.revision
            or (expected_revision_id is not None and current["revision_id"] != expected_revision_id)
        ):
            result = EventResult(
                EventOutcome.STALE,
                f"command targets {definition.command_name}@{command.revision}; "
                f"current stage/revision is {current['stage']}/"
                f"{definition.command_name}@{current['revision_no']}",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result

        if command.action is CommandAction.APPROVE:
            return self._approve(connection, current, comment, definition)
        if command.action is CommandAction.REQUEST_CHANGES:
            return self._request_changes(
                connection, current, comment, command.feedback, definition
            )
        return self._address_with_agent(connection, current, comment, definition)

    def _approve(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
        definition: ArtifactDefinition,
    ) -> EventResult:
        review = self._pending_review(connection, current["revision_id"])
        if current["revision_status"] != ArtifactStatus.REVIEW or not review:
            result = EventResult(
                EventOutcome.STALE,
                f"{definition.command_name}@{current['revision_no']} is not awaiting review",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        now = utc_now()
        updated = connection.execute(
            """UPDATE features SET stage = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND stage = ? AND version = ?""",
            (
                definition.next_stage,
                now,
                current["feature_id"],
                definition.stage,
                current["version"],
            ),
        ).rowcount
        if updated != 1:
            return EventResult(EventOutcome.STALE, "feature changed concurrently", current["feature_id"])
        connection.execute(
            "UPDATE artifact_revisions SET status = 'APPROVED' WHERE id = ? AND status = 'REVIEW'",
            (current["revision_id"],),
        )
        connection.execute(
            """UPDATE reviews SET status = 'APPROVED', reviewer_external_id = ?,
                      source_event_id = ?, decided_at = ?
               WHERE id = ? AND status = 'PENDING'""",
            (comment.actor_id, comment.id, now, review["id"]),
        )
        if definition.next_artifact_type is not None:
            self._create_artifact_generation(
                connection,
                feature_id=current["feature_id"],
                definition=ARTIFACT_DEFINITIONS[definition.next_artifact_type],
                expected_feature_version=current["version"] + 1,
                source_revision_ids=(current["revision_id"],),
                idempotency_suffix=current["revision_id"],
            )
        else:
            # The next stage is modeled and queued even when its worker is not
            # part of the current walking slice.
            self._insert_job(
                connection,
                kind=definition.next_job,
                idempotency_key=(
                    f"generate:{current['feature_id']}:"
                    f"{definition.next_stage.value.lower()}:{current['revision_id']}"
                ),
                payload={
                    "feature_id": current["feature_id"],
                    f"approved_{definition.command_name}_revision_id": current["revision_id"],
                    "expected_feature_version": current["version"] + 1,
                },
            )
        self._insert_outbox(
            connection,
            kind="STAGE_UPDATED",
            external_feature_id=current["external_id"],
            idempotency_key=(
                f"feature:{current['feature_id']}:stage:"
                f"{definition.next_stage.value.lower()}:v{current['version'] + 1}"
            ),
            payload={"stage": definition.next_stage, "attention": "enzo:running"},
        )
        self._domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="ARTIFACT_APPROVED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="ARTIFACT_REVISION",
            subject_id=current["revision_id"],
            payload={
                "artifact_type": definition.artifact_type,
                "revision": current["revision_no"],
                "next_stage": definition.next_stage,
            },
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"{definition.display_name} approved; {definition.next_stage} generation queued",
            current["feature_id"],
        )

    def _request_changes(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
        feedback: tuple[FeedbackDraft, ...],
        definition: ArtifactDefinition,
    ) -> EventResult:
        review = self._pending_review(connection, current["revision_id"])
        if current["revision_status"] != ArtifactStatus.REVIEW or not review:
            result = EventResult(
                EventOutcome.STALE,
                f"{definition.command_name}@{current['revision_no']} is not awaiting review",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        if not feedback:
            result = EventResult(
                EventOutcome.REJECTED,
                "request-changes requires at least one '- [Section] comment' item",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        if any(not item.comment.strip() for item in feedback):
            result = EventResult(
                EventOutcome.REJECTED,
                "feedback comments cannot be empty",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        content = (
            self.artifact_store.read_content(current["content_path"])
            if current["content_path"]
            else ""
        )
        for item in feedback:
            if item.selection and not self._selection_matches(content, item.selection):
                result = EventResult(
                    EventOutcome.REJECTED,
                    "feedback selection does not match this immutable revision",
                    current["feature_id"],
                )
                self._insert_rejection_notice(connection, current, comment.id, result.message)
                return result
        now = utc_now()
        updated = connection.execute(
            """UPDATE features SET version = version + 1, updated_at = ?
               WHERE id = ? AND stage = ? AND version = ?""",
            (now, current["feature_id"], definition.stage, current["version"]),
        ).rowcount
        if updated != 1:
            return EventResult(EventOutcome.STALE, "feature changed concurrently", current["feature_id"])
        connection.execute(
            "UPDATE artifact_revisions SET status = 'CHANGES_REQUESTED' WHERE id = ? AND status = 'REVIEW'",
            (current["revision_id"],),
        )
        connection.execute(
            """UPDATE reviews SET status = 'CHANGES_REQUESTED', reviewer_external_id = ?,
                      source_event_id = ?, decided_at = ?
               WHERE id = ? AND status = 'PENDING'""",
            (comment.actor_id, comment.id, now, review["id"]),
        )
        for ordinal, item in enumerate(feedback, start=1):
            connection.execute(
                """INSERT INTO feedback_items
                   (id, review_id, artifact_revision_id, source_comment_id,
                    source_ordinal, section, comment, status, created_at, location_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
                (
                    new_id(),
                    review["id"],
                    current["revision_id"],
                    comment.id,
                    ordinal,
                    item.section,
                    item.comment,
                    now,
                    canonical_json(asdict(item.selection)) if item.selection else None,
                ),
            )
        self._insert_outbox(
            connection,
            kind="CHANGES_REQUESTED",
            external_feature_id=current["external_id"],
            idempotency_key=f"review:{review['id']}:changes-requested",
            payload={
                "revision": current["revision_no"],
                "artifact": definition.display_name,
                "feedback_count": len(feedback),
                "attention": "enzo:changes-requested",
            },
        )
        self._domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="CHANGES_REQUESTED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="ARTIFACT_REVISION",
            subject_id=current["revision_id"],
            payload={
                "artifact_type": definition.artifact_type,
                "feedback_count": len(feedback),
            },
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"recorded {len(feedback)} feedback item(s)",
            current["feature_id"],
        )

    @staticmethod
    def _selection_matches(content: str, selection: TextSelection) -> bool:
        if (
            selection.start_offset < 0
            or selection.end_offset <= selection.start_offset
            or selection.end_offset > len(content)
            or content[selection.start_offset : selection.end_offset] != selection.exact
        ):
            return False
        prefix_start = max(0, selection.start_offset - len(selection.prefix))
        suffix_end = selection.end_offset + len(selection.suffix)
        return (
            content[prefix_start : selection.start_offset] == selection.prefix
            and content[selection.end_offset : suffix_end] == selection.suffix
        )

    def _address_with_agent(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
        definition: ArtifactDefinition,
    ) -> EventResult:
        if current["revision_status"] != ArtifactStatus.CHANGES_REQUESTED:
            result = EventResult(
                EventOutcome.STALE,
                f"{definition.command_name}@{current['revision_no']} has no requested changes to address",
                current["feature_id"],
            )
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        feedback = connection.execute(
            """SELECT id FROM feedback_items
               WHERE artifact_revision_id = ? AND status = 'OPEN'
               ORDER BY created_at, source_ordinal""",
            (current["revision_id"],),
        ).fetchall()
        if not feedback:
            result = EventResult(EventOutcome.STALE, "no unresolved feedback exists", current["feature_id"])
            self._insert_rejection_notice(connection, current, comment.id, result.message)
            return result
        feedback_ids = [row["id"] for row in feedback]
        feedback_hash = hashlib.sha256("\n".join(feedback_ids).encode()).hexdigest()[:16]
        now = utc_now()
        revision_id = new_id()
        next_revision = current["revision_no"] + 1
        updated = connection.execute(
            """UPDATE features SET version = version + 1, updated_at = ?
               WHERE id = ? AND stage = ? AND version = ?""",
            (now, current["feature_id"], definition.stage, current["version"]),
        ).rowcount
        if updated != 1:
            return EventResult(EventOutcome.STALE, "feature changed concurrently", current["feature_id"])
        connection.execute(
            """INSERT INTO artifact_revisions
               (id, artifact_id, revision_no, parent_revision_id, status,
                created_by_kind, created_by_id, created_at)
               VALUES (?, ?, ?, ?, 'UPDATING', 'AGENT', ?, ?)""",
            (
                revision_id,
                current["artifact_id"],
                next_revision,
                current["revision_id"],
                comment.actor_id,
                now,
            ),
        )
        connection.execute(
            "UPDATE artifacts SET current_revision_id = ? WHERE id = ? AND current_revision_id = ?",
            (revision_id, current["artifact_id"], current["revision_id"]),
        )
        self._insert_job(
            connection,
            kind=definition.revise_job,
            idempotency_key=f"resolve:{current['revision_id']}:{feedback_hash}",
            payload={
                "feature_id": current["feature_id"],
                "revision_id": revision_id,
                "parent_revision_id": current["revision_id"],
                "feedback_ids": feedback_ids,
                "artifact_type": definition.artifact_type,
                "previous_artifact_revision_ids": self._previous_revision_ids(
                    connection, current["feature_id"], definition
                ),
                "expected_feature_version": current["version"] + 1,
            },
        )
        self._insert_outbox(
            connection,
            kind="ARTIFACT_UPDATING",
            external_feature_id=current["external_id"],
            idempotency_key=f"artifact:{revision_id}:updating",
            payload={
                "artifact": definition.display_name,
                "revision": next_revision,
                "attention": "enzo:running",
            },
        )
        self._domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="AGENT_FEEDBACK_RESOLUTION_REQUESTED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="ARTIFACT_REVISION",
            subject_id=revision_id,
            payload={
                "artifact_type": definition.artifact_type,
                "parent_revision_id": current["revision_id"],
                "feedback_ids": feedback_ids,
            },
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"{definition.command_name}@{next_revision} agent update queued",
            current["feature_id"],
        )

    def submit_manual_revision(
        self,
        *,
        external_feature_id: str,
        actor_id: str,
        base_revision: int,
        content: str,
        delivery_id: str,
        artifact_type: ArtifactType | None = None,
        execution_plan: dict[str, Any] | None = None,
    ) -> EventResult:
        if actor_id not in self.reviewer_ids:
            return EventResult(EventOutcome.REJECTED, "actor is not an allowed reviewer")
        now = utc_now()
        with self.database.transaction() as connection:
            if artifact_type is None:
                feature_stage = connection.execute(
                    "SELECT stage FROM features WHERE external_id = ?", (external_feature_id,)
                ).fetchone()
                if not feature_stage or feature_stage["stage"] not in {
                    definition.stage for definition in ARTIFACT_DEFINITIONS.values()
                }:
                    return EventResult(EventOutcome.STALE, "feature has no editable artifact stage")
                artifact_type = ArtifactType(feature_stage["stage"])
            definition = ARTIFACT_DEFINITIONS[artifact_type]
            validate_artifact(content, definition)
            normalized_execution_plan = None
            extra_files = None
            if definition.artifact_type is ArtifactType.PLAN:
                normalized_execution_plan = normalize_execution_plan(execution_plan)
                extra_files = {
                    "execution-plan.yaml": execution_plan_yaml(normalized_execution_plan)
                }
            duplicate = self._begin_event(
                connection,
                delivery_id=delivery_id,
                event_type="MANUAL_REVISION_SUBMITTED",
                payload={
                    "external_feature_id": external_feature_id,
                    "actor_id": actor_id,
                    "artifact_type": artifact_type,
                    "base_revision": base_revision,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                },
            )
            if duplicate:
                return duplicate
            current = connection.execute(
                """SELECT f.id AS feature_id, f.external_id, f.stage, f.version,
                          a.id AS artifact_id, r.id AS revision_id,
                          r.revision_no, r.status AS revision_status
                   FROM features f JOIN artifacts a ON a.feature_id = f.id AND a.type = ?
                   JOIN artifact_revisions r ON r.id = a.current_revision_id
                   WHERE f.external_id = ?""",
                (artifact_type, external_feature_id),
            ).fetchone()
            if (
                not current
                or current["stage"] != definition.stage
                or current["revision_no"] != base_revision
                or current["revision_status"] != ArtifactStatus.CHANGES_REQUESTED
            ):
                result = EventResult(EventOutcome.STALE, "manual edit does not target current requested changes")
                self._finish_event(connection, delivery_id, result, rejected=True)
                return result
            revision_id = new_id()
            next_revision = base_revision + 1
            connection.execute(
                """INSERT INTO artifact_revisions
                   (id, artifact_id, revision_no, parent_revision_id, status,
                    created_by_kind, created_by_id, created_at)
                   VALUES (?, ?, ?, ?, 'UPDATING', 'HUMAN', ?, ?)""",
                (revision_id, current["artifact_id"], next_revision, current["revision_id"], actor_id, now),
            )
            connection.execute(
                "UPDATE artifacts SET current_revision_id = ? WHERE id = ?",
                (revision_id, current["artifact_id"]),
            )
            connection.execute(
                "UPDATE features SET version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (now, current["feature_id"], current["version"]),
            )
            stored = self.artifact_store.store_markdown(
                artifact_type=definition.artifact_type,
                display_name=definition.display_name,
                feature_id=current["feature_id"],
                revision_id=revision_id,
                revision_no=next_revision,
                parent_revision_id=current["revision_id"],
                created_by=actor_id,
                content=content,
                extra_files=extra_files,
            )
            connection.execute(
                """UPDATE artifact_revisions
                   SET status = 'REVIEW', content_path = ?, manifest_path = ?, content_sha256 = ?
                   WHERE id = ? AND status = 'UPDATING'""",
                (str(stored.content_path), str(stored.manifest_path), stored.content_sha256, revision_id),
            )
            review_id = new_id()
            connection.execute(
                "INSERT INTO reviews (id, artifact_revision_id, status, opened_at) VALUES (?, ?, 'PENDING', ?)",
                (review_id, revision_id, now),
            )
            if normalized_execution_plan is not None:
                self._persist_execution_plan(
                    connection,
                    feature_id=current["feature_id"],
                    revision_id=revision_id,
                    plan=normalized_execution_plan,
                    plan_path=str(stored.extra_paths["execution-plan.yaml"]),
                    plan_sha256=stored.extra_sha256["execution-plan.yaml"],
                )
            connection.execute(
                """UPDATE feedback_items
                   SET status = 'RESOLVED', resolution_type = ?, resolved_by = ?,
                       resolved_in_revision_id = ?, resolved_at = ?
                   WHERE artifact_revision_id = ? AND status = 'OPEN'""",
                (ResolutionType.MANUAL, actor_id, revision_id, now, current["revision_id"]),
            )
            self._insert_review_outbox(
                connection,
                external_feature_id=current["external_id"],
                feature_id=current["feature_id"],
                review_id=review_id,
                revision_id=revision_id,
                revision_no=next_revision,
                addressed_count=connection.execute(
                    "SELECT count(*) AS n FROM feedback_items WHERE resolved_in_revision_id = ?",
                    (revision_id,),
                ).fetchone()["n"],
                definition=definition,
            )
            result = EventResult(
                EventOutcome.APPLIED,
                f"manual {definition.command_name}@{next_revision} awaits review",
                current["feature_id"],
            )
            self._finish_event(connection, delivery_id, result)
            return result

    def _create_artifact_generation(
        self,
        connection: sqlite3.Connection,
        *,
        feature_id: str,
        definition: ArtifactDefinition,
        expected_feature_version: int,
        source_revision_ids: tuple[str, ...],
        idempotency_suffix: str,
    ) -> str:
        now = utc_now()
        artifact_id = new_id()
        revision_id = new_id()
        connection.execute(
            """INSERT INTO artifacts
               (id, feature_id, type, display_name, current_revision_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                feature_id,
                definition.artifact_type,
                definition.display_name,
                revision_id,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO artifact_revisions
               (id, artifact_id, revision_no, status, created_by_kind,
                created_by_id, created_at)
               VALUES (?, ?, 1, 'GENERATING', 'SYSTEM', 'orchestrator', ?)""",
            (revision_id, artifact_id, now),
        )
        self._insert_job(
            connection,
            kind=definition.generate_job,
            idempotency_key=(
                f"generate:{feature_id}:{definition.command_name}:{idempotency_suffix}"
            ),
            payload={
                "feature_id": feature_id,
                "artifact_type": definition.artifact_type,
                "revision_id": revision_id,
                "previous_artifact_revision_ids": list(source_revision_ids),
                "expected_feature_version": expected_feature_version,
            },
        )
        return revision_id

    @staticmethod
    def _previous_revision_ids(
        connection: sqlite3.Connection,
        feature_id: str,
        definition: ArtifactDefinition,
    ) -> list[str]:
        if definition.previous_artifact_type is None:
            return []
        row = connection.execute(
            """SELECT r.id FROM artifacts a
               JOIN artifact_revisions r ON r.id = a.current_revision_id
               WHERE a.feature_id = ? AND a.type = ? AND r.status = 'APPROVED'""",
            (feature_id, definition.previous_artifact_type),
        ).fetchone()
        return [row["id"]] if row else []

    @staticmethod
    def _persist_execution_plan(
        connection: sqlite3.Connection,
        *,
        feature_id: str,
        revision_id: str,
        plan: dict[str, Any],
        plan_path: str,
        plan_sha256: str,
    ) -> str:
        execution_plan_id = new_id()
        connection.execute(
            """INSERT INTO execution_plans
               (id, feature_id, artifact_revision_id, schema_version,
                plan_path, plan_sha256, created_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (execution_plan_id, feature_id, revision_id, plan_path, plan_sha256, utc_now()),
        )
        for position, task in enumerate(plan["tasks"], start=1):
            connection.execute(
                """INSERT INTO execution_tasks
                   (id, execution_plan_id, task_key, position, title, description,
                    depends_on_json, verification_refs_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id(),
                    execution_plan_id,
                    task["key"],
                    position,
                    task["title"],
                    task["description"],
                    canonical_json(task["depends_on"]),
                    canonical_json(task["verification_refs"]),
                ),
            )
        return execution_plan_id

    @staticmethod
    def _read_execution_plan(
        connection: sqlite3.Connection, revision_id: str | None
    ) -> dict[str, Any] | None:
        if not revision_id:
            return None
        plan = connection.execute(
            "SELECT * FROM execution_plans WHERE artifact_revision_id = ?", (revision_id,)
        ).fetchone()
        if not plan:
            return None
        tasks = connection.execute(
            """SELECT * FROM execution_tasks
               WHERE execution_plan_id = ? ORDER BY position""",
            (plan["id"],),
        ).fetchall()
        return {
            "schema_version": plan["schema_version"],
            "tasks": [
                {
                    "key": task["task_key"],
                    "title": task["title"],
                    "description": task["description"],
                    "depends_on": json.loads(task["depends_on_json"]),
                    "verification_refs": json.loads(task["verification_refs_json"]),
                }
                for task in tasks
            ],
        }

    def claim_next_job(self, *, worker_id: str, lease_seconds: float = 60) -> ClaimedJob | None:
        now = time.time()
        placeholders = ",".join("?" for _ in self.supported_job_kinds)
        with self.database.transaction() as connection:
            row = connection.execute(
                f"""SELECT * FROM jobs
                    WHERE kind IN ({placeholders})
                      AND available_at <= ?
                      AND (status = 'PENDING' OR (status = 'RUNNING' AND lease_expires_at < ?))
                    ORDER BY created_at, id LIMIT 1""",
                (*self.supported_job_kinds, now, now),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE jobs SET status = 'RUNNING', attempts = attempts + 1,
                          lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND (status = 'PENDING' OR lease_expires_at < ?)""",
                (worker_id, now + lease_seconds, utc_now(), row["id"], now),
            ).rowcount
            if updated != 1:
                return None
            return ClaimedJob(
                id=row["id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"] + 1,
                lease_owner=worker_id,
            )

    def run_one_job(self, *, worker_id: str = "worker-1") -> str | None:
        job = self.claim_next_job(worker_id=worker_id)
        if not job:
            return None
        self.execute_claimed_job(job)
        return job.id

    def execute_claimed_job(self, job: ClaimedJob) -> None:
        if job.kind not in JOB_DEFINITIONS:
            if self.execution_coordinator and job.kind in self.execution_coordinator.supported_job_kinds:
                self.execution_coordinator.execute(job)
                return
            self._cancel_stale_job(job, f"unsupported job kind: {job.kind}")
            return
        definition, is_revision = JOB_DEFINITIONS[job.kind]
        context = self._load_agent_context(job)
        if context is None:
            self._cancel_stale_job(job, "job preconditions are no longer true")
            return
        agent_run_id = new_id()
        request = AgentRequest(
            role=AgentRole.FEEDBACK_RESOLVER if is_revision else definition.generate_role,
            workload=(
                AgentWorkload.ARTIFACT_REVISION
                if is_revision
                else AgentWorkload.ARTIFACT_GENERATION
            ),
            project_id=self.project.id,
            feature_id=context["feature_id"],
            artifact_revision_id=context["revision_id"],
            title=context["title"],
            current_content=context.get("current_content"),
            unresolved_feedback=tuple(context["feedback"]),
            metadata={
                "job_id": job.id,
                "attempt": job.attempts,
                "artifact_type": definition.artifact_type,
                "artifact": definition.display_name,
                "required_headings": list(definition.required_headings),
                "previous_artifacts": context["previous_artifacts"],
                "current_execution_plan": context.get("current_execution_plan"),
            },
        )
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO agent_runs
                   (id, role, workload, artifact_revision_id, status, request_json, started_at)
                   VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)""",
                (
                    agent_run_id,
                    request.role,
                    request.workload,
                    request.artifact_revision_id,
                    canonical_json(
                        {
                            "project_id": request.project_id,
                            "feature_id": request.feature_id,
                            "artifact_revision_id": request.artifact_revision_id,
                            "workload": request.workload,
                            "title": request.title,
                            "feedback": [asdict(item) for item in request.unresolved_feedback],
                            "metadata": request.metadata,
                        }
                    ),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE artifact_revisions SET agent_run_id = ? WHERE id = ?",
                (agent_run_id, request.artifact_revision_id),
            )
        try:
            result = self.agent_runner.run(request)
            validate_artifact(result.content, definition)
            if definition.artifact_type is ArtifactType.PLAN:
                normalize_execution_plan(result.metadata.get("execution_plan"))
            self._apply_agent_result(job, agent_run_id, result)
        except BaseException as exc:
            self._fail_agent_job(job, agent_run_id, exc)
            raise

    def _load_agent_context(self, job: ClaimedJob) -> dict[str, Any] | None:
        definition, is_revision = JOB_DEFINITIONS[job.kind]
        with self.database.read() as connection:
            lease = connection.execute(
                "SELECT status, attempts, lease_owner FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if (
                not lease
                or lease["status"] != JobStatus.RUNNING
                or lease["attempts"] != job.attempts
                or lease["lease_owner"] != job.lease_owner
            ):
                return None
            row = connection.execute(
                """SELECT f.id AS feature_id, f.title, f.version, f.external_id, f.stage,
                          a.current_revision_id, a.type AS artifact_type,
                          r.id AS revision_id, r.revision_no,
                          r.parent_revision_id, r.status, p.content_path AS parent_content_path
                   FROM features f
                   JOIN artifacts a ON a.feature_id = f.id AND a.type = ?
                   JOIN artifact_revisions r ON r.id = ? AND r.artifact_id = a.id
                   LEFT JOIN artifact_revisions p ON p.id = r.parent_revision_id
                   WHERE f.id = ?""",
                (definition.artifact_type, job.payload["revision_id"], job.payload["feature_id"]),
            ).fetchone()
            expected_status = "UPDATING" if is_revision else "GENERATING"
            if (
                not row
                or row["stage"] != definition.stage
                or row["version"] != job.payload["expected_feature_version"]
                or row["current_revision_id"] != row["revision_id"]
                or row["status"] != expected_status
            ):
                return None
            feedback_rows = []
            if is_revision:
                ids = job.payload["feedback_ids"]
                placeholders = ",".join("?" for _ in ids)
                feedback_rows = connection.execute(
                    f"""SELECT section, comment, location_json FROM feedback_items
                        WHERE id IN ({placeholders}) AND status = 'OPEN'
                        ORDER BY created_at, source_ordinal""",
                    ids,
                ).fetchall()
                if len(feedback_rows) != len(ids):
                    return None
            previous_artifacts: dict[str, str] = {}
            for revision_id in job.payload.get("previous_artifact_revision_ids", []):
                previous = connection.execute(
                    """SELECT a.display_name, r.content_path, r.status
                       FROM artifact_revisions r
                       JOIN artifacts a ON a.id = r.artifact_id
                       WHERE r.id = ? AND a.feature_id = ?""",
                    (revision_id, row["feature_id"]),
                ).fetchone()
                if not previous or previous["status"] != ArtifactStatus.APPROVED:
                    return None
                previous_artifacts[previous["display_name"]] = self.artifact_store.read_content(
                    previous["content_path"]
                )
            current_execution_plan = None
            if is_revision and definition.artifact_type is ArtifactType.PLAN:
                current_execution_plan = self._read_execution_plan(
                    connection, row["parent_revision_id"]
                )
                if current_execution_plan is None:
                    return None
            return {
                **dict(row),
                "current_content": (
                    self.artifact_store.read_content(row["parent_content_path"])
                    if row["parent_content_path"]
                    else None
                ),
                "feedback": [
                    FeedbackDraft(
                        item["section"],
                        item["comment"],
                        TextSelection(**json.loads(item["location_json"]))
                        if item["location_json"]
                        else None,
                    )
                    for item in feedback_rows
                ],
                "previous_artifacts": previous_artifacts,
                "current_execution_plan": current_execution_plan,
            }

    def _apply_agent_result(
        self,
        job: ClaimedJob,
        agent_run_id: str,
        result: AgentResult,
    ) -> None:
        run_info = require_run_info(result)
        content = result.content
        result_metadata = result.metadata
        definition, is_revision = JOB_DEFINITIONS[job.kind]
        execution_plan = None
        extra_files = None
        if definition.artifact_type is ArtifactType.PLAN:
            execution_plan = normalize_execution_plan(result_metadata.get("execution_plan"))
            extra_files = {"execution-plan.yaml": execution_plan_yaml(execution_plan)}
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT f.id AS feature_id, f.external_id, f.version, f.stage,
                          a.current_revision_id, r.id AS revision_id, r.revision_no,
                          r.parent_revision_id, r.status
                   FROM features f
                   JOIN artifacts a ON a.feature_id = f.id AND a.type = ?
                   JOIN artifact_revisions r ON r.id = ? AND r.artifact_id = a.id
                   WHERE f.id = ?""",
                (definition.artifact_type, job.payload["revision_id"], job.payload["feature_id"]),
            ).fetchone()
            expected_status = "UPDATING" if is_revision else "GENERATING"
            job_row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
            if (
                not row
                or not job_row
                or job_row["status"] != JobStatus.RUNNING
                or job_row["lease_owner"] != job.lease_owner
                or row["stage"] != definition.stage
                or row["version"] != job.payload["expected_feature_version"]
                or row["current_revision_id"] != row["revision_id"]
                or row["status"] != expected_status
            ):
                connection.execute(
                    """UPDATE agent_runs
                       SET status = 'SUPERSEDED', runner_name = ?, runner_kind = ?,
                           provider = ?, model = ?, result_json = ?, finished_at = ?
                       WHERE id = ?""",
                    (
                        run_info.runner_name,
                        run_info.runner_kind,
                        run_info.provider,
                        run_info.model,
                        canonical_json(result.metadata),
                        now,
                        agent_run_id,
                    ),
                )
                connection.execute(
                    """UPDATE jobs SET status = 'CANCELLED', updated_at = ?, last_error = ?,
                              lease_owner = NULL, lease_expires_at = NULL
                       WHERE id = ? AND lease_owner = ?""",
                    (now, "agent result became stale", job.id, job.lease_owner),
                )
                return
            stored = self.artifact_store.store_markdown(
                artifact_type=definition.artifact_type,
                display_name=definition.display_name,
                feature_id=row["feature_id"],
                revision_id=row["revision_id"],
                revision_no=row["revision_no"],
                parent_revision_id=row["parent_revision_id"],
                created_by=agent_run_id,
                content=content,
                extra_files=extra_files,
            )
            updated = connection.execute(
                """UPDATE features SET version = version + 1, updated_at = ?
                   WHERE id = ? AND version = ? AND stage = ?""",
                (now, row["feature_id"], row["version"], definition.stage),
            ).rowcount
            if updated != 1:
                raise ConcurrentTransitionError("feature changed while applying agent result")
            connection.execute(
                """UPDATE artifact_revisions SET status = 'REVIEW', content_path = ?,
                          manifest_path = ?, content_sha256 = ?
                   WHERE id = ? AND status = ?""",
                (
                    str(stored.content_path),
                    str(stored.manifest_path),
                    stored.content_sha256,
                    row["revision_id"],
                    expected_status,
                ),
            )
            review_id = new_id()
            connection.execute(
                "INSERT INTO reviews (id, artifact_revision_id, status, opened_at) VALUES (?, ?, 'PENDING', ?)",
                (review_id, row["revision_id"], now),
            )
            if execution_plan is not None:
                self._persist_execution_plan(
                    connection,
                    feature_id=row["feature_id"],
                    revision_id=row["revision_id"],
                    plan=execution_plan,
                    plan_path=str(stored.extra_paths["execution-plan.yaml"]),
                    plan_sha256=stored.extra_sha256["execution-plan.yaml"],
                )
            addressed_count = 0
            if is_revision:
                feedback_ids = job.payload["feedback_ids"]
                placeholders = ",".join("?" for _ in feedback_ids)
                addressed_count = connection.execute(
                    f"""UPDATE feedback_items SET status = 'RESOLVED', resolution_type = 'AGENT',
                               resolved_by = ?, resolved_in_revision_id = ?, resolved_at = ?
                        WHERE id IN ({placeholders}) AND status = 'OPEN'""",
                    (agent_run_id, row["revision_id"], now, *feedback_ids),
                ).rowcount
            connection.execute(
                """UPDATE agent_runs
                   SET status = 'SUCCEEDED', output_sha256 = ?, runner_name = ?,
                       runner_kind = ?, provider = ?, model = ?, result_json = ?,
                       finished_at = ?
                   WHERE id = ? AND status = 'RUNNING'""",
                (
                    stored.content_sha256,
                    run_info.runner_name,
                    run_info.runner_kind,
                    run_info.provider,
                    run_info.model,
                    canonical_json(result.metadata),
                    now,
                    agent_run_id,
                ),
            )
            connection.execute(
                """UPDATE jobs SET status = 'SUCCEEDED', updated_at = ?, lease_owner = NULL,
                          lease_expires_at = NULL WHERE id = ? AND lease_owner = ?""",
                (now, job.id, job.lease_owner),
            )
            self._insert_review_outbox(
                connection,
                external_feature_id=row["external_id"],
                feature_id=row["feature_id"],
                review_id=review_id,
                revision_id=row["revision_id"],
                revision_no=row["revision_no"],
                addressed_count=addressed_count,
                definition=definition,
            )
            self._domain_event(
                connection,
                feature_id=row["feature_id"],
                event_type="ARTIFACT_REVISION_READY_FOR_REVIEW",
                actor_type="AGENT",
                actor_id=agent_run_id,
                subject_type="ARTIFACT_REVISION",
                subject_id=row["revision_id"],
                payload={
                    "artifact_type": definition.artifact_type,
                    "revision": row["revision_no"],
                    "addressed_count": addressed_count,
                },
            )

    def _fail_agent_job(self, job: ClaimedJob, agent_run_id: str, error: BaseException) -> None:
        now = utc_now()
        message = f"{type(error).__name__}: {error}"
        runner_name = error.runner_name if isinstance(error, AgentRunnerError) else None
        failure_kind = error.kind if isinstance(error, AgentRunnerError) else None
        retryable = int(error.retryable) if isinstance(error, AgentRunnerError) else None
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE agent_runs
                   SET status = 'FAILED', runner_name = COALESCE(?, runner_name),
                       failure_kind = ?, retryable = ?, error = ?, finished_at = ?
                   WHERE id = ?""",
                (
                    runner_name,
                    failure_kind,
                    retryable,
                    message,
                    now,
                    agent_run_id,
                ),
            )
            connection.execute(
                "UPDATE artifact_revisions SET status = 'FAILED' WHERE id = ? AND status IN ('GENERATING', 'UPDATING')",
                (job.payload["revision_id"],),
            )
            connection.execute(
                """UPDATE jobs SET status = 'FAILED', last_error = ?, updated_at = ?,
                          lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = ? AND lease_owner = ?""",
                (message, now, job.id, job.lease_owner),
            )
            feature = connection.execute(
                "SELECT id, external_id, version FROM features WHERE id = ?",
                (job.payload["feature_id"],),
            ).fetchone()
            if feature:
                connection.execute(
                    "UPDATE features SET version = version + 1, updated_at = ? WHERE id = ?",
                    (now, feature["id"]),
                )
                self._insert_outbox(
                    connection,
                    kind="ARTIFACT_FAILED",
                    external_feature_id=feature["external_id"],
                    idempotency_key=f"job:{job.id}:failed",
                    payload={"job_id": job.id, "error": message, "attention": "enzo:failed"},
                )

    def _cancel_stale_job(self, job: ClaimedJob, reason: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE jobs SET status = 'CANCELLED', last_error = ?, updated_at = ?,
                          lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = ? AND lease_owner = ?""",
                (reason, utc_now(), job.id, job.lease_owner),
            )

    def claim_next_outbox(self, *, worker_id: str, lease_seconds: float = 30) -> OutboxMessage | None:
        now = time.time()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM outbox_messages
                   WHERE available_at <= ?
                     AND (status = 'PENDING' OR (status = 'SENDING' AND lease_expires_at < ?))
                   ORDER BY created_at, id LIMIT 1""",
                (now, now),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE outbox_messages SET status = 'SENDING', attempts = attempts + 1,
                          lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND (status = 'PENDING' OR lease_expires_at < ?)""",
                (worker_id, now + lease_seconds, utc_now(), row["id"], now),
            ).rowcount
            if updated != 1:
                return None
            return OutboxMessage(
                id=row["id"],
                kind=row["kind"],
                external_feature_id=row["external_feature_id"],
                idempotency_key=row["idempotency_key"],
                payload=json.loads(row["payload_json"]),
            )

    def run_one_outbox(self, *, worker_id: str = "outbox-1") -> str | None:
        message = self.claim_next_outbox(worker_id=worker_id)
        if not message:
            return None
        try:
            external_id = self.task_manager.deliver(message)
        except BaseException as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE outbox_messages SET status = 'PENDING', available_at = ?,
                              lease_owner = NULL, lease_expires_at = NULL,
                              last_error = ?, updated_at = ?
                       WHERE id = ? AND status = 'SENDING'""",
                    (time.time(), f"{type(exc).__name__}: {exc}", utc_now(), message.id),
                )
            return message.id
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE outbox_messages SET status = 'SENT', external_message_id = ?,
                          lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'SENDING'""",
                (external_id, utc_now(), message.id),
            )
        return message.id

    def drain(self, *, worker_id: str = "worker-1", max_steps: int = 100) -> int:
        steps = 0
        while steps < max_steps:
            progressed = False
            if self.run_one_job(worker_id=worker_id):
                progressed = True
                steps += 1
            if self.run_one_outbox(worker_id=f"{worker_id}-outbox"):
                progressed = True
                steps += 1
            if not progressed:
                break
        return steps

    def feature_snapshot(self, external_feature_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            feature = connection.execute(
                "SELECT * FROM features WHERE external_id = ?", (external_feature_id,)
            ).fetchone()
            if not feature:
                return None
            artifacts = connection.execute(
                """SELECT a.id AS artifact_id, a.type, a.display_name, a.current_revision_id,
                          r.revision_no, r.status AS artifact_status, r.content_path,
                          r.content_sha256
                   FROM artifacts a JOIN artifact_revisions r ON r.id = a.current_revision_id
                   WHERE a.feature_id = ?
                   ORDER BY a.created_at, a.id""",
                (feature["id"],),
            ).fetchall()
            artifact_by_type = {row["type"]: row for row in artifacts}
            current_artifact = artifact_by_type.get(feature["stage"])
            if current_artifact is None and artifacts:
                current_artifact = artifacts[-1]
            feedback_count = connection.execute(
                """SELECT count(*) AS n FROM feedback_items f
                   WHERE f.artifact_revision_id = ? AND f.status = 'OPEN'""",
                (current_artifact["current_revision_id"] if current_artifact else "",),
            ).fetchone()["n"]
            execution = connection.execute(
                """SELECT * FROM executions WHERE feature_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (feature["id"],),
            ).fetchone()
            execution_detail = dict(execution) if execution else None
            if execution_detail is not None:
                execution_detail["result"] = json.loads(
                    execution_detail.pop("result_json") or "{}"
                )
                evidence = connection.execute(
                    """SELECT implementation_revision_id, evidence_type, name, status,
                              exit_code, duration_seconds, log_path, log_sha256
                       FROM verification_evidence WHERE execution_id = ? ORDER BY created_at, name""",
                    (execution["id"],),
                ).fetchall()
                execution_detail["evidence"] = [dict(item) for item in evidence]
                revisions = connection.execute(
                    """SELECT * FROM implementation_revisions
                       WHERE execution_id = ? ORDER BY revision_no""",
                    (execution["id"],),
                ).fetchall()
                execution_detail["implementation_revisions"] = [
                    dict(item) for item in revisions
                ]
            return {
                "id": feature["id"],
                "external_id": feature["external_id"],
                "title": feature["title"],
                "stage": feature["stage"],
                "version": feature["version"],
                "artifact": dict(current_artifact) if current_artifact else None,
                "artifacts": [dict(row) for row in artifacts],
                "unresolved_feedback": feedback_count,
                "execution": execution_detail,
                "execution_plan": (
                    self._read_execution_plan(
                        connection,
                        current_artifact["current_revision_id"] if current_artifact else None,
                    )
                    if current_artifact and current_artifact["type"] == ArtifactType.PLAN
                    else None
                ),
            }

    def artifact_history(
        self,
        external_feature_id: str,
        artifact_type: ArtifactType | str = ArtifactType.INTENT,
    ) -> list[dict[str, Any]]:
        artifact_type = ArtifactType(artifact_type)
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT r.* FROM artifact_revisions r
                   JOIN artifacts a ON a.id = r.artifact_id
                   JOIN features f ON f.id = a.feature_id
                   WHERE f.external_id = ? AND a.type = ?
                   ORDER BY r.revision_no""",
                (external_feature_id, artifact_type),
            ).fetchall()
            return [dict(row) for row in rows]

    def revision_detail(self, revision_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            revision = connection.execute(
                """SELECT r.*, a.type AS artifact_type, a.display_name,
                          f.external_id, f.title, f.stage AS feature_stage
                   FROM artifact_revisions r
                   JOIN artifacts a ON a.id = r.artifact_id
                   JOIN features f ON f.id = a.feature_id WHERE r.id = ?""",
                (revision_id,),
            ).fetchone()
            if not revision:
                return None
            feedback = connection.execute(
                "SELECT * FROM feedback_items WHERE artifact_revision_id = ? ORDER BY source_ordinal",
                (revision_id,),
            ).fetchall()
            detail = dict(revision)
            detail["feedback"] = []
            for item in feedback:
                feedback_detail = dict(item)
                feedback_detail["location"] = json.loads(
                    feedback_detail.pop("location_json") or "null"
                )
                detail["feedback"].append(feedback_detail)
            resolved_feedback = connection.execute(
                """SELECT * FROM feedback_items
                   WHERE resolved_in_revision_id = ?
                   ORDER BY created_at, source_ordinal""",
                (revision_id,),
            ).fetchall()
            detail["resolved_feedback"] = []
            for item in resolved_feedback:
                feedback_detail = dict(item)
                feedback_detail["location"] = json.loads(
                    feedback_detail.pop("location_json") or "null"
                )
                detail["resolved_feedback"].append(feedback_detail)
            detail["execution_plan"] = self._read_execution_plan(connection, revision_id)
            plan_file = connection.execute(
                "SELECT plan_path, plan_sha256 FROM execution_plans WHERE artifact_revision_id = ?",
                (revision_id,),
            ).fetchone()
            detail["execution_plan_content"] = (
                self.artifact_store.read_content(plan_file["plan_path"]) if plan_file else None
            )
            detail["execution_plan_sha256"] = plan_file["plan_sha256"] if plan_file else None
            detail["content"] = (
                self.artifact_store.read_content(revision["content_path"])
                if revision["content_path"]
                else None
            )
            return detail

    def execution_detail(self, execution_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            execution = connection.execute(
                """SELECT e.*, f.external_id, f.title
                   FROM executions e JOIN features f ON f.id = e.feature_id
                   WHERE e.id = ?""",
                (execution_id,),
            ).fetchone()
            if not execution:
                return None
            detail = dict(execution)
            detail["result"] = json.loads(detail.pop("result_json") or "{}")
            evidence = connection.execute(
                """SELECT * FROM verification_evidence
                   WHERE execution_id = ? ORDER BY created_at, name""",
                (execution_id,),
            ).fetchall()
            detail["evidence"] = [
                {
                    **dict(item),
                    "log": Path(item["log_path"]).read_text(encoding="utf-8"),
                }
                for item in evidence
            ]
            revisions = connection.execute(
                """SELECT * FROM implementation_revisions
                   WHERE execution_id = ? ORDER BY revision_no""",
                (execution_id,),
            ).fetchall()
            detail["implementation_revisions"] = []
            for revision in revisions:
                item = dict(revision)
                item["diff"] = (
                    Path(revision["diff_path"]).read_text(encoding="utf-8")
                    if revision["diff_path"]
                    else None
                )
                item["feedback"] = [
                    dict(feedback)
                    for feedback in connection.execute(
                        """SELECT * FROM implementation_feedback_items
                           WHERE implementation_revision_id = ?
                           ORDER BY source_ordinal""",
                        (revision["id"],),
                    ).fetchall()
                ]
                item["resolved_feedback"] = [
                    dict(feedback)
                    for feedback in connection.execute(
                        """SELECT * FROM implementation_feedback_items
                           WHERE resolved_in_revision_id = ?
                           ORDER BY created_at, source_ordinal""",
                        (revision["id"],),
                    ).fetchall()
                ]
                detail["implementation_revisions"].append(item)
            return detail

    def execution_detail_for_revision(self, revision_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            execution = connection.execute(
                "SELECT execution_id FROM implementation_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
        return self.execution_detail(execution["execution_id"]) if execution else None

    @staticmethod
    def _pending_review(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM reviews WHERE artifact_revision_id = ? AND status = 'PENDING'",
            (revision_id,),
        ).fetchone()

    @staticmethod
    def _begin_event(
        connection: sqlite3.Connection,
        *,
        delivery_id: str,
        event_type: str,
        payload: dict[str, Any],
        source: str = "TASK_MANAGER",
    ) -> EventResult | None:
        existing = connection.execute(
            "SELECT outcome, message FROM inbox_events WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if existing:
            return EventResult(EventOutcome.DUPLICATE, existing["message"] or "duplicate event")
        connection.execute(
            """INSERT INTO inbox_events
               (id, source, delivery_id, event_type, payload_json, status, received_at)
               VALUES (?, ?, ?, ?, ?, 'PROCESSING', ?)""",
            (new_id(), source, delivery_id, event_type, canonical_json(payload), utc_now()),
        )
        return None

    @staticmethod
    def _finish_event(
        connection: sqlite3.Connection,
        delivery_id: str,
        result: EventResult,
        *,
        rejected: bool = False,
    ) -> None:
        connection.execute(
            """UPDATE inbox_events SET status = ?, outcome = ?, message = ?, processed_at = ?
               WHERE delivery_id = ?""",
            (
                "REJECTED" if rejected else "PROCESSED",
                result.outcome,
                result.message,
                utc_now(),
                delivery_id,
            ),
        )

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> str:
        job_id = new_id()
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO jobs
               (id, kind, idempotency_key, payload_json, status, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (job_id, kind, idempotency_key, canonical_json(payload), time.time(), now, now),
        )
        return job_id

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        kind: str,
        external_feature_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> str:
        message_id = new_id()
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO outbox_messages
               (id, kind, external_feature_id, idempotency_key, payload_json,
                status, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                message_id,
                kind,
                external_feature_id,
                idempotency_key,
                canonical_json(payload),
                time.time(),
                now,
                now,
            ),
        )
        return message_id

    def _insert_review_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        external_feature_id: str,
        feature_id: str,
        review_id: str,
        revision_id: str,
        revision_no: int,
        addressed_count: int,
        definition: ArtifactDefinition,
    ) -> None:
        self._insert_outbox(
            connection,
            kind="REVIEW_REQUIRED",
            external_feature_id=external_feature_id,
            idempotency_key=f"review:{review_id}:required",
            payload={
                "feature_id": feature_id,
                "artifact": definition.display_name,
                "artifact_type": definition.artifact_type,
                "revision_id": revision_id,
                "revision": revision_no,
                "addressed_feedback_count": addressed_count,
                "attention": "enzo:needs-review",
            },
        )

    def _insert_rejection_notice(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        source_event_id: str,
        reason: str,
    ) -> None:
        self._insert_outbox(
            connection,
            kind="COMMAND_REJECTED",
            external_feature_id=current["external_id"],
            idempotency_key=f"command:{source_event_id}:rejected",
            payload={"reason": reason},
        )

    @staticmethod
    def _domain_event(
        connection: sqlite3.Connection,
        *,
        feature_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO domain_events
               (id, feature_id, event_type, actor_type, actor_id,
                subject_type, subject_id, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id(),
                feature_id,
                event_type,
                actor_type,
                actor_id,
                subject_type,
                subject_id,
                canonical_json(payload),
                utc_now(),
            ),
        )


class ConcurrentTransitionError(RuntimeError):
    pass

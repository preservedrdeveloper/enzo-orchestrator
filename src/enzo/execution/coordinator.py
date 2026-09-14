from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..agents import require_run_info
from ..artifacts.store import ArtifactStore
from ..database import Database
from ..domain import (
    AgentRequest,
    AgentRole,
    AgentWorkload,
    ClaimedJob,
    CommandAction,
    EventOutcome,
    EventResult,
    ExternalComment,
    FeedbackDraft,
    JobStatus,
    ResolutionType,
    ReviewCommand,
)
from ..ports import AgentRunner
from .commands import CommandRunner
from .git import GitWorkspaceManager
from .models import CommandResult, PreparedWorkspace, ProjectConfig

PREPARE_IMPLEMENTATION = "PREPARE_IMPLEMENTATION"
RUN_IMPLEMENTATION = "RUN_IMPLEMENTATION"
VERIFY_IMPLEMENTATION = "VERIFY_IMPLEMENTATION"
CLOSE_IMPLEMENTATION = "CLOSE_IMPLEMENTATION"
EXECUTION_JOB_KINDS = (
    PREPARE_IMPLEMENTATION,
    RUN_IMPLEMENTATION,
    VERIFY_IMPLEMENTATION,
    CLOSE_IMPLEMENTATION,
)


class ExecutionCoordinator:
    """Coordinates implementation while deterministic code owns every Git transition."""

    supported_job_kinds = EXECUTION_JOB_KINDS

    def __init__(
        self,
        *,
        database: Database,
        project: ProjectConfig,
        agent_runner: AgentRunner,
        git: GitWorkspaceManager,
        commands: CommandRunner,
        evidence_root: Path,
    ) -> None:
        if not project.execution_enabled:
            raise ValueError("execution coordinator requires a repository-bound project")
        self.database = database
        self.project = project
        self.agent_runner = agent_runner
        self.git = git
        self.commands = commands
        self.evidence_root = evidence_root.resolve()

    def handle_review_command(
        self,
        connection: sqlite3.Connection,
        *,
        comment: ExternalComment,
        command: ReviewCommand,
    ) -> EventResult:
        current = self._current_review_context(connection, comment.feature_id)
        if current is None:
            return EventResult(
                EventOutcome.REJECTED,
                "feature has no implementation revision under Enzo review",
            )
        if current["revision_no"] != command.revision:
            return EventResult(
                EventOutcome.STALE,
                f"command targets implementation@{command.revision}; "
                f"current revision is implementation@{current['revision_no']}",
                current["feature_id"],
            )
        if command.action is CommandAction.APPROVE:
            return self._approve_implementation(connection, current, comment)
        if command.action is CommandAction.REQUEST_CHANGES:
            return self._request_implementation_changes(
                connection,
                current,
                comment,
                command.feedback,
            )
        return self._address_implementation_with_agent(connection, current, comment)

    def _approve_implementation(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
    ) -> EventResult:
        if (
            current["feature_stage"] != "VERIFICATION"
            or current["revision_status"] != "REVIEW"
            or current["review_id"] is None
        ):
            return EventResult(
                EventOutcome.STALE,
                f"implementation@{current['revision_no']} is not awaiting review",
                current["feature_id"],
            )
        now = _utc_now()
        updated = connection.execute(
            """UPDATE features SET version = version + 1, updated_at = ?
               WHERE id = ? AND stage = 'VERIFICATION' AND version = ?""",
            (now, current["feature_id"], current["feature_version"]),
        ).rowcount
        if updated != 1:
            return EventResult(
                EventOutcome.STALE,
                "feature changed concurrently",
                current["feature_id"],
            )
        connection.execute(
            """UPDATE implementation_revisions SET status = 'APPROVED'
               WHERE id = ? AND status = 'REVIEW'""",
            (current["revision_id"],),
        )
        connection.execute(
            """UPDATE implementation_reviews
               SET status = 'APPROVED', reviewer_external_id = ?, source_event_id = ?,
                   decided_at = ?
               WHERE id = ? AND status = 'PENDING'""",
            (comment.actor_id, comment.id, now, current["review_id"]),
        )
        self._insert_job(
            connection,
            kind=CLOSE_IMPLEMENTATION,
            idempotency_key=f"implementation:{current['revision_id']}:close",
            payload={
                "feature_id": current["feature_id"],
                "execution_id": current["execution_id"],
                "implementation_revision_id": current["revision_id"],
                "expected_feature_version": current["feature_version"] + 1,
            },
        )
        self._insert_domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="IMPLEMENTATION_APPROVED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="IMPLEMENTATION_REVISION",
            subject_id=current["revision_id"],
            payload={"revision": current["revision_no"]},
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"implementation@{current['revision_no']} approved; closure queued",
            current["feature_id"],
        )

    def _request_implementation_changes(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
        feedback: tuple[FeedbackDraft, ...],
    ) -> EventResult:
        if not feedback:
            return EventResult(
                EventOutcome.REJECTED,
                "request-changes requires at least one structured feedback item",
                current["feature_id"],
            )
        if (
            current["feature_stage"] != "VERIFICATION"
            or current["revision_status"] != "REVIEW"
            or current["review_id"] is None
        ):
            return EventResult(
                EventOutcome.STALE,
                f"implementation@{current['revision_no']} is not awaiting review",
                current["feature_id"],
            )
        now = _utc_now()
        updated = connection.execute(
            """UPDATE features SET stage = 'IMPLEMENTATION', version = version + 1,
                      updated_at = ?
               WHERE id = ? AND stage = 'VERIFICATION' AND version = ?""",
            (now, current["feature_id"], current["feature_version"]),
        ).rowcount
        if updated != 1:
            return EventResult(
                EventOutcome.STALE,
                "feature changed concurrently",
                current["feature_id"],
            )
        connection.execute(
            """UPDATE implementation_revisions SET status = 'CHANGES_REQUESTED'
               WHERE id = ? AND status = 'REVIEW'""",
            (current["revision_id"],),
        )
        connection.execute(
            """UPDATE implementation_reviews
               SET status = 'CHANGES_REQUESTED', reviewer_external_id = ?,
                   source_event_id = ?, decided_at = ?
               WHERE id = ? AND status = 'PENDING'""",
            (comment.actor_id, comment.id, now, current["review_id"]),
        )
        for ordinal, item in enumerate(feedback, start=1):
            connection.execute(
                """INSERT INTO implementation_feedback_items
                   (id, review_id, implementation_revision_id, source_comment_id,
                    source_ordinal, section, comment, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                (
                    _new_id(),
                    current["review_id"],
                    current["revision_id"],
                    comment.id,
                    ordinal,
                    item.section,
                    item.comment,
                    now,
                ),
            )
        self._insert_outbox(
            connection,
            kind="STAGE_UPDATED",
            external_feature_id=current["external_id"],
            idempotency_key=(
                f"feature:{current['feature_id']}:stage:implementation:"
                f"v{current['feature_version'] + 1}"
            ),
            payload={"stage": "IMPLEMENTATION", "attention": "enzo:changes-requested"},
        )
        self._insert_outbox(
            connection,
            kind="IMPLEMENTATION_CHANGES_REQUESTED",
            external_feature_id=current["external_id"],
            idempotency_key=f"implementation:{current['revision_id']}:changes-requested",
            payload={
                "revision": current["revision_no"],
                "feedback_count": len(feedback),
                "attention": "enzo:changes-requested",
            },
        )
        self._insert_domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="IMPLEMENTATION_CHANGES_REQUESTED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="IMPLEMENTATION_REVISION",
            subject_id=current["revision_id"],
            payload={
                "revision": current["revision_no"],
                "feedback_count": len(feedback),
            },
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"{len(feedback)} implementation feedback item(s) recorded",
            current["feature_id"],
        )

    def _address_implementation_with_agent(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        comment: ExternalComment,
    ) -> EventResult:
        if (
            current["feature_stage"] != "IMPLEMENTATION"
            or current["revision_status"] != "CHANGES_REQUESTED"
            or current["execution_status"] != "SUCCEEDED"
        ):
            return EventResult(
                EventOutcome.STALE,
                f"implementation@{current['revision_no']} has no addressable feedback",
                current["feature_id"],
            )
        feedback = connection.execute(
            """SELECT id FROM implementation_feedback_items
               WHERE implementation_revision_id = ? AND status = 'OPEN'
               ORDER BY created_at, source_ordinal""",
            (current["revision_id"],),
        ).fetchall()
        if not feedback:
            return EventResult(
                EventOutcome.REJECTED,
                "implementation revision has no unresolved feedback",
                current["feature_id"],
            )
        now = _utc_now()
        revision_id = _new_id()
        revision_no = current["revision_no"] + 1
        connection.execute(
            """INSERT INTO implementation_revisions
               (id, execution_id, revision_no, parent_revision_id, status, base_sha,
                created_by_kind, created_by_id, created_at)
               VALUES (?, ?, ?, ?, 'UPDATING', ?, 'AGENT', 'pending', ?)""",
            (
                revision_id,
                current["execution_id"],
                revision_no,
                current["revision_id"],
                current["base_sha"],
                now,
            ),
        )
        connection.execute(
            """UPDATE executions SET status = 'RUNNING', current_revision_id = ?,
                      finished_at = NULL, updated_at = ?, error = NULL
               WHERE id = ? AND status = 'SUCCEEDED'""",
            (revision_id, now, current["execution_id"]),
        )
        self._insert_job(
            connection,
            kind=RUN_IMPLEMENTATION,
            idempotency_key=f"implementation:{revision_id}:run",
            payload={
                "execution_id": current["execution_id"],
                "feature_id": current["feature_id"],
                "plan_revision_id": current["plan_revision_id"],
                "implementation_revision_id": revision_id,
                "feedback_ids": [item["id"] for item in feedback],
                "expected_feature_version": current["feature_version"],
            },
        )
        self._insert_domain_event(
            connection,
            feature_id=current["feature_id"],
            event_type="IMPLEMENTATION_REVISION_QUEUED",
            actor_type="HUMAN",
            actor_id=comment.actor_id,
            subject_type="IMPLEMENTATION_REVISION",
            subject_id=revision_id,
            payload={
                "revision": revision_no,
                "parent_revision_id": current["revision_id"],
                "feedback_count": len(feedback),
            },
        )
        return EventResult(
            EventOutcome.APPLIED,
            f"implementation@{revision_no} agent revision queued",
            current["feature_id"],
        )

    @staticmethod
    def _current_review_context(
        connection: sqlite3.Connection,
        external_feature_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT f.id AS feature_id, f.external_id, f.stage AS feature_stage,
                      f.version AS feature_version, e.id AS execution_id,
                      e.status AS execution_status, e.base_sha, e.head_sha, e.branch,
                      e.worktree_path, ep.artifact_revision_id AS plan_revision_id,
                      ir.id AS revision_id, ir.revision_no,
                      ir.status AS revision_status, ir.head_sha AS revision_head_sha,
                      review.id AS review_id
               FROM features f
               JOIN executions e ON e.feature_id = f.id
               JOIN execution_plans ep ON ep.id = e.execution_plan_id
               JOIN implementation_revisions ir ON ir.id = e.current_revision_id
               LEFT JOIN implementation_reviews review
                 ON review.implementation_revision_id = ir.id
                AND review.status = 'PENDING'
               WHERE f.external_id = ?
               ORDER BY e.created_at DESC LIMIT 1""",
            (external_feature_id,),
        ).fetchone()

    def execute(self, job: ClaimedJob) -> None:
        try:
            if job.kind == PREPARE_IMPLEMENTATION:
                self._prepare(job)
            elif job.kind == RUN_IMPLEMENTATION:
                self._run_coder(job)
            elif job.kind == VERIFY_IMPLEMENTATION:
                self._verify(job)
            elif job.kind == CLOSE_IMPLEMENTATION:
                self._close(job)
            else:
                raise ValueError(f"unsupported execution job: {job.kind}")
        except BaseException as error:
            self._fail(job, error)
            raise

    def _prepare(self, job: ClaimedJob) -> None:
        with self.database.transaction() as connection:
            context = self._load_prepare_context(connection, job)
            if context is None:
                self._cancel_job(connection, job, "implementation preconditions are stale")
                return
            execution = connection.execute(
                """SELECT * FROM executions
                   WHERE feature_id = ? AND execution_plan_id = ? AND attempt = 1""",
                (context["feature_id"], context["execution_plan_id"]),
            ).fetchone()
            if execution is None:
                execution_id = _new_id()
                now = _utc_now()
                connection.execute(
                    """INSERT INTO executions
                       (id, feature_id, execution_plan_id, project_id, status, attempt,
                        agent, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'PENDING', 1, ?, ?, ?)""",
                    (
                        execution_id,
                        context["feature_id"],
                        context["execution_plan_id"],
                        self.project.id,
                        "unassigned",
                        now,
                        now,
                    ),
                )
            else:
                execution_id = execution["id"]

        workspace = self.git.prepare(
            self.project,
            execution_id=execution_id,
            external_feature_id=context["external_id"],
        )
        bootstrap = next(
            (command for command in self.project.commands if command.name == "bootstrap"),
            None,
        )
        if bootstrap:
            result = self.commands.run(bootstrap, cwd=workspace.worktree_path)
            self._store_evidence(execution_id, result, evidence_type="BOOTSTRAP")
            if result.exit_code != 0:
                raise ExecutionCommandError(result)

        with self.database.transaction() as connection:
            if not self._job_is_current(connection, job):
                return
            now = _utc_now()
            execution = connection.execute(
                "SELECT current_revision_id FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
            implementation_revision_id = execution["current_revision_id"]
            if not implementation_revision_id:
                implementation_revision_id = _new_id()
                connection.execute(
                    """INSERT INTO implementation_revisions
                       (id, execution_id, revision_no, status, base_sha,
                        created_by_kind, created_by_id, created_at)
                       VALUES (?, ?, 1, 'UPDATING', ?, 'AGENT', 'pending', ?)""",
                    (implementation_revision_id, execution_id, workspace.base_sha, now),
                )
            connection.execute(
                """UPDATE executions
                   SET status = 'RUNNING', base_sha = ?, branch = ?, worktree_path = ?,
                       current_revision_id = ?, started_at = COALESCE(started_at, ?),
                       updated_at = ?, error = NULL
                   WHERE id = ? AND status IN ('PENDING', 'RUNNING')""",
                (
                    workspace.base_sha,
                    workspace.branch,
                    str(workspace.worktree_path),
                    implementation_revision_id,
                    now,
                    now,
                    execution_id,
                ),
            )
            self._insert_job(
                connection,
                kind=RUN_IMPLEMENTATION,
                idempotency_key=f"execution:{execution_id}:run",
                payload={
                    "execution_id": execution_id,
                    "feature_id": context["feature_id"],
                    "plan_revision_id": context["plan_revision_id"],
                    "implementation_revision_id": implementation_revision_id,
                    "expected_feature_version": context["feature_version"],
                },
            )
            self._succeed_job(connection, job)

    def _run_coder(self, job: ClaimedJob) -> None:
        with self.database.read() as connection:
            context = self._load_execution_context(connection, job)
        if context is None:
            with self.database.transaction() as connection:
                self._cancel_job(connection, job, "coding preconditions are stale")
            return
        is_revision = bool(job.payload.get("feedback_ids"))
        request = AgentRequest(
            role=AgentRole.FEEDBACK_RESOLVER if is_revision else AgentRole.CODER,
            workload=(
                AgentWorkload.IMPLEMENTATION_REVISION
                if is_revision
                else AgentWorkload.IMPLEMENTATION
            ),
            project_id=self.project.id,
            feature_id=context["feature_id"],
            artifact_revision_id=context["plan_revision_id"],
            title=context["title"],
            workspace_path=context["worktree_path"],
            unresolved_feedback=tuple(context["feedback"]),
            metadata={
                "execution_id": context["execution_id"],
                "execution_plan": context["execution_plan"],
                "approved_spec": context["approved_spec"],
                "approved_plan": context["approved_plan"],
                "target_kind": "IMPLEMENTATION",
                "implementation_revision": context["implementation_revision_no"],
            },
        )
        result = self.agent_runner.run(request)
        run_info = require_run_info(result)
        with self.database.transaction() as connection:
            if not self._job_is_current(connection, job):
                return
            previous_result = json.loads(context["result_json"] or "{}")
            agent_runs = previous_result.setdefault("agent_runs", [])
            agent_runs.append(
                {
                    "role": request.role,
                    "workload": request.workload,
                    "runner": {
                        "name": run_info.runner_name,
                        "kind": run_info.runner_kind,
                        "provider": run_info.provider,
                        "model": run_info.model,
                    },
                    "summary": result.content,
                    "metadata": result.metadata,
                    "implementation_revision": context["implementation_revision_no"],
                }
            )
            connection.execute(
                """UPDATE executions
                   SET agent = ?, result_json = ?, updated_at = ? WHERE id = ?""",
                (
                    run_info.runner_name,
                    _canonical_json(previous_result),
                    _utc_now(),
                    context["execution_id"],
                ),
            )
            self._insert_job(
                connection,
                kind=VERIFY_IMPLEMENTATION,
                idempotency_key=(
                    f"implementation:{context['implementation_revision_id']}:verify"
                ),
                payload=job.payload,
            )
            self._succeed_job(connection, job)

    def _verify(self, job: ClaimedJob) -> None:
        with self.database.read() as connection:
            context = self._load_execution_context(connection, job)
        if context is None:
            with self.database.transaction() as connection:
                self._cancel_job(connection, job, "verification preconditions are stale")
            return
        workspace = PreparedWorkspace(
            checkout_path=Path(),
            worktree_path=Path(context["worktree_path"]),
            base_sha=context["base_sha"],
            branch=context["branch"],
        )
        for command in self.project.commands:
            if command.name == "bootstrap":
                continue
            result = self.commands.run(command, cwd=workspace.worktree_path)
            self._store_evidence(
                context["execution_id"],
                result,
                implementation_revision_id=context["implementation_revision_id"],
                evidence_type=command.name.upper(),
            )
            if result.exit_code != 0:
                raise ExecutionCommandError(result)

        head_sha = self.git.commit_and_push(
            self.project,
            workspace,
            message=(
                f"enzo: implement {context['external_id']} "
                f"revision {context['implementation_revision_no']}"
            ),
        )
        if context["parent_head_sha"] and head_sha == context["parent_head_sha"]:
            raise NoImplementationChangesError(
                "implementation revision produced no commit after its parent"
            )
        diff_stat = self.git.diff_stat(workspace)
        diff_path, diff_sha256 = self._store_implementation_diff(
            context["execution_id"],
            context["implementation_revision_no"],
            self.git.diff_patch(workspace),
        )
        with self.database.transaction() as connection:
            if not self._job_is_current(connection, job):
                return
            now = _utc_now()
            previous_result = json.loads(context["result_json"] or "{}")
            previous_result["diff_stat"] = diff_stat
            updated = connection.execute(
                """UPDATE features SET stage = 'VERIFICATION', version = version + 1,
                          updated_at = ?
                   WHERE id = ? AND stage = 'IMPLEMENTATION' AND version = ?""",
                (now, context["feature_id"], context["feature_version"]),
            ).rowcount
            if updated != 1:
                raise RuntimeError("feature changed while publishing implementation review")
            connection.execute(
                """UPDATE implementation_revisions
                   SET status = 'REVIEW', head_sha = ?, diff_path = ?, diff_sha256 = ?,
                       created_by_id = ?
                   WHERE id = ? AND status = 'UPDATING'""",
                (
                    head_sha,
                    str(diff_path),
                    diff_sha256,
                    context["agent"],
                    context["implementation_revision_id"],
                ),
            )
            review_id = _new_id()
            connection.execute(
                """INSERT INTO implementation_reviews
                   (id, implementation_revision_id, status, opened_at)
                   VALUES (?, ?, 'PENDING', ?)""",
                (review_id, context["implementation_revision_id"], now),
            )
            connection.execute(
                """UPDATE executions SET status = 'SUCCEEDED', head_sha = ?, result_json = ?,
                          finished_at = ?, updated_at = ?, error = NULL
                   WHERE id = ? AND status = 'RUNNING'""",
                (
                    head_sha,
                    _canonical_json(previous_result),
                    now,
                    now,
                    context["execution_id"],
                ),
            )
            feedback_ids = job.payload.get("feedback_ids", [])
            if feedback_ids:
                placeholders = ",".join("?" for _ in feedback_ids)
                connection.execute(
                    f"""UPDATE implementation_feedback_items
                         SET status = 'RESOLVED', resolution_type = ?, resolved_by = ?,
                             resolved_in_revision_id = ?, resolved_at = ?
                         WHERE id IN ({placeholders}) AND status = 'OPEN'""",
                    (
                        ResolutionType.AGENT,
                        context["agent"],
                        context["implementation_revision_id"],
                        now,
                        *feedback_ids,
                    ),
                )
            self._insert_outbox(
                connection,
                kind="STAGE_UPDATED",
                external_feature_id=context["external_id"],
                idempotency_key=(
                    f"feature:{context['feature_id']}:stage:verification:"
                    f"v{context['feature_version'] + 1}"
                ),
                payload={"stage": "VERIFICATION", "attention": "enzo:needs-review"},
            )
            self._insert_outbox(
                connection,
                kind="IMPLEMENTATION_REVIEW_REQUIRED",
                external_feature_id=context["external_id"],
                idempotency_key=(
                    f"implementation:{context['implementation_revision_id']}:review-required"
                ),
                payload={
                    "execution_id": context["execution_id"],
                    "implementation_revision_id": context[
                        "implementation_revision_id"
                    ],
                    "revision": context["implementation_revision_no"],
                    "branch": context["branch"],
                    "base_sha": context["base_sha"],
                    "head_sha": head_sha,
                    "diff_stat": diff_stat,
                    "attention": "enzo:needs-review",
                },
            )
            self._insert_domain_event(
                connection,
                feature_id=context["feature_id"],
                event_type="IMPLEMENTATION_REVISION_READY_FOR_REVIEW",
                actor_type="AGENT",
                actor_id=context["agent"],
                subject_type="IMPLEMENTATION_REVISION",
                subject_id=context["implementation_revision_id"],
                payload={
                    "revision": context["implementation_revision_no"],
                    "head_sha": head_sha,
                },
            )
            self._succeed_job(connection, job)

    def _close(self, job: ClaimedJob) -> None:
        with self.database.read() as connection:
            if not self._job_is_current(connection, job):
                return
            context = connection.execute(
                """SELECT e.id AS execution_id, e.status AS execution_status,
                          e.branch, e.worktree_path, e.head_sha, f.id AS feature_id,
                          f.external_id, f.stage AS feature_stage,
                          f.version AS feature_version, ir.id AS revision_id,
                          ir.status AS revision_status
                   FROM executions e
                   JOIN features f ON f.id = e.feature_id
                   JOIN implementation_revisions ir ON ir.id = e.current_revision_id
                   WHERE e.id = ? AND f.id = ? AND ir.id = ? AND e.project_id = ?""",
                (
                    job.payload["execution_id"],
                    job.payload["feature_id"],
                    job.payload["implementation_revision_id"],
                    self.project.id,
                ),
            ).fetchone()
        if (
            context is None
            or context["execution_status"] != "SUCCEEDED"
            or context["revision_status"] != "APPROVED"
            or context["feature_stage"] != "VERIFICATION"
            or context["feature_version"] != job.payload["expected_feature_version"]
        ):
            with self.database.transaction() as connection:
                self._cancel_job(connection, job, "closure preconditions are stale")
            return

        remote_head = self.git.remote_head(self.project, branch=context["branch"])
        if remote_head != context["head_sha"]:
            raise RemoteHeadMismatchError(
                f"remote branch head {remote_head!r} does not match "
                f"approved head {context['head_sha']!r}"
            )
        if self.project.cleanup_on_done:
            self.git.cleanup(
                self.project,
                worktree_path=Path(context["worktree_path"]),
            )

        with self.database.transaction() as connection:
            if not self._job_is_current(connection, job):
                return
            now = _utc_now()
            updated = connection.execute(
                """UPDATE features SET stage = 'DONE', version = version + 1,
                          updated_at = ?
                   WHERE id = ? AND stage = 'VERIFICATION' AND version = ?""",
                (now, context["feature_id"], context["feature_version"]),
            ).rowcount
            if updated != 1:
                raise RuntimeError("feature changed while closing implementation")
            connection.execute(
                """UPDATE executions SET status = 'CLOSED', finished_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'SUCCEEDED'""",
                (now, now, context["execution_id"]),
            )
            self._insert_outbox(
                connection,
                kind="STAGE_UPDATED",
                external_feature_id=context["external_id"],
                idempotency_key=(
                    f"feature:{context['feature_id']}:stage:done:"
                    f"v{context['feature_version'] + 1}"
                ),
                payload={"stage": "DONE", "attention": "enzo:done"},
            )
            self._insert_domain_event(
                connection,
                feature_id=context["feature_id"],
                event_type="EXECUTION_CLOSED",
                actor_type="SYSTEM",
                actor_id="execution-coordinator",
                subject_type="EXECUTION",
                subject_id=context["execution_id"],
                payload={
                    "head_sha": context["head_sha"],
                    "worktree_cleaned": self.project.cleanup_on_done,
                },
            )
            self._succeed_job(connection, job)

    def _load_prepare_context(
        self, connection: sqlite3.Connection, job: ClaimedJob
    ) -> sqlite3.Row | None:
        if not self._job_is_current(connection, job):
            return None
        return connection.execute(
            """SELECT f.id AS feature_id, f.external_id, f.version AS feature_version,
                      ep.id AS execution_plan_id, ep.artifact_revision_id AS plan_revision_id
               FROM features f
               JOIN execution_plans ep
                 ON ep.artifact_revision_id = ? AND ep.feature_id = f.id
               JOIN artifact_revisions r ON r.id = ep.artifact_revision_id
               WHERE f.id = ? AND f.project_id = ? AND f.stage = 'IMPLEMENTATION'
                 AND f.version = ? AND r.status = 'APPROVED'""",
            (
                job.payload["approved_plan_revision_id"],
                job.payload["feature_id"],
                self.project.id,
                job.payload["expected_feature_version"],
            ),
        ).fetchone()

    def _load_execution_context(
        self, connection: sqlite3.Connection, job: ClaimedJob
    ) -> dict[str, Any] | None:
        if not self._job_is_current(connection, job):
            return None
        row = connection.execute(
            """SELECT e.id AS execution_id, e.*, f.external_id, f.title,
                      f.version AS feature_version,
                      ep.artifact_revision_id AS plan_revision_id,
                      ir.id AS implementation_revision_id,
                      ir.revision_no AS implementation_revision_no,
                      ir.parent_revision_id,
                      parent.head_sha AS parent_head_sha,
                      plan.content_path AS plan_path,
                      spec.content_path AS spec_path
               FROM executions e
               JOIN features f ON f.id = e.feature_id
               JOIN execution_plans ep ON ep.id = e.execution_plan_id
               JOIN implementation_revisions ir
                 ON ir.id = e.current_revision_id AND ir.id = ?
               LEFT JOIN implementation_revisions parent ON parent.id = ir.parent_revision_id
               JOIN artifact_revisions plan ON plan.id = ep.artifact_revision_id
               JOIN artifacts spec_artifact
                 ON spec_artifact.feature_id = f.id AND spec_artifact.type = 'SPEC'
               JOIN artifact_revisions spec ON spec.id = spec_artifact.current_revision_id
               WHERE e.id = ? AND e.feature_id = ? AND e.project_id = ?
                 AND e.status = 'RUNNING' AND f.stage = 'IMPLEMENTATION'
                 AND f.version = ? AND plan.status = 'APPROVED'
                 AND spec.status = 'APPROVED' AND ir.status = 'UPDATING'""",
            (
                job.payload["implementation_revision_id"],
                job.payload["execution_id"],
                job.payload["feature_id"],
                self.project.id,
                job.payload["expected_feature_version"],
            ),
        ).fetchone()
        if row is None:
            return None
        tasks = connection.execute(
            """SELECT task_key, title, description, depends_on_json, verification_refs_json
               FROM execution_tasks WHERE execution_plan_id = ? ORDER BY position""",
            (row["execution_plan_id"],),
        ).fetchall()
        result = dict(row)
        feedback_rows: list[sqlite3.Row] = []
        feedback_ids = job.payload.get("feedback_ids", [])
        if feedback_ids:
            placeholders = ",".join("?" for _ in feedback_ids)
            feedback_rows = connection.execute(
                f"""SELECT section, comment FROM implementation_feedback_items
                     WHERE id IN ({placeholders}) AND status = 'OPEN'
                     ORDER BY created_at, source_ordinal""",
                feedback_ids,
            ).fetchall()
            if len(feedback_rows) != len(feedback_ids):
                return None
        result["feedback"] = [
            FeedbackDraft(item["section"], item["comment"]) for item in feedback_rows
        ]
        result["execution_plan"] = {
            "schema_version": 1,
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
        result["approved_plan"] = ArtifactStore.read_content(row["plan_path"])
        result["approved_spec"] = ArtifactStore.read_content(row["spec_path"])
        return result

    def _store_evidence(
        self,
        execution_id: str,
        result: CommandResult,
        *,
        implementation_revision_id: str | None = None,
        evidence_type: str,
    ) -> None:
        evidence_scope = implementation_revision_id or "execution"
        evidence_dir = self.evidence_root / execution_id / "evidence" / evidence_scope
        evidence_dir.mkdir(parents=True, exist_ok=True)
        log_path = evidence_dir / f"{result.name}.log"
        log_content = (
            f"command: {_canonical_json(list(result.argv))}\n"
            f"exit_code: {result.exit_code}\n"
            f"duration_seconds: {result.duration_seconds:.6f}\n\n"
            f"{result.output}"
        )
        log_path.write_text(log_content, encoding="utf-8")
        digest = hashlib.sha256(log_content.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """SELECT id FROM verification_evidence
                   WHERE execution_id = ? AND implementation_revision_id IS ?
                     AND name = ?""",
                (execution_id, implementation_revision_id, result.name),
            ).fetchone()
            values = (
                evidence_type,
                "PASS" if result.exit_code == 0 else "FAIL",
                _canonical_json(list(result.argv)),
                result.exit_code,
                result.duration_seconds,
                str(log_path),
                digest,
                _utc_now(),
            )
            if existing:
                connection.execute(
                    """UPDATE verification_evidence
                       SET evidence_type = ?, status = ?, command_json = ?, exit_code = ?,
                           duration_seconds = ?, log_path = ?, log_sha256 = ?, created_at = ?
                       WHERE id = ?""",
                    (*values, existing["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO verification_evidence
                       (id, execution_id, implementation_revision_id, evidence_type,
                        name, status, command_json, exit_code, duration_seconds,
                        log_path, log_sha256, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _new_id(),
                        execution_id,
                        implementation_revision_id,
                        evidence_type,
                        result.name,
                        values[1],
                        *values[2:],
                    ),
                )

    def _store_implementation_diff(
        self,
        execution_id: str,
        revision_no: int,
        content: str,
    ) -> tuple[Path, str]:
        directory = self.evidence_root / execution_id / "implementation" / f"r{revision_no:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "diff.patch"
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return path, digest

    def _fail(self, job: ClaimedJob, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"
        with self.database.transaction() as connection:
            if not self._job_is_current(connection, job):
                return
            execution_id = job.payload.get("execution_id")
            if not execution_id and job.payload.get("approved_plan_revision_id"):
                execution = connection.execute(
                    """SELECT e.id FROM executions e
                       JOIN execution_plans ep ON ep.id = e.execution_plan_id
                       WHERE e.feature_id = ? AND ep.artifact_revision_id = ?
                       ORDER BY e.attempt DESC LIMIT 1""",
                    (
                        job.payload["feature_id"],
                        job.payload["approved_plan_revision_id"],
                    ),
                ).fetchone()
                execution_id = execution["id"] if execution else None
            if execution_id:
                connection.execute(
                    """UPDATE executions SET status = 'FAILED', error = ?, finished_at = ?,
                              updated_at = ? WHERE id = ? AND status IN ('PENDING', 'RUNNING')""",
                    (message, _utc_now(), _utc_now(), execution_id),
                )
            implementation_revision_id = job.payload.get("implementation_revision_id")
            if implementation_revision_id:
                connection.execute(
                    """UPDATE implementation_revisions SET status = 'FAILED'
                       WHERE id = ? AND status = 'UPDATING'""",
                    (implementation_revision_id,),
                )
            connection.execute(
                """UPDATE jobs SET status = 'FAILED', last_error = ?, updated_at = ?,
                          lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = ? AND lease_owner = ?""",
                (message, _utc_now(), job.id, job.lease_owner),
            )
            feature = connection.execute(
                "SELECT external_id FROM features WHERE id = ?", (job.payload["feature_id"],)
            ).fetchone()
            if feature:
                self._insert_outbox(
                    connection,
                    kind="IMPLEMENTATION_FAILED",
                    external_feature_id=feature["external_id"],
                    idempotency_key=f"job:{job.id}:failed",
                    payload={"job_id": job.id, "error": message, "attention": "enzo:failed"},
                )

    @staticmethod
    def _job_is_current(connection: sqlite3.Connection, job: ClaimedJob) -> bool:
        row = connection.execute(
            "SELECT status, attempts, lease_owner FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
        return bool(
            row
            and row["status"] == JobStatus.RUNNING
            and row["attempts"] == job.attempts
            and row["lease_owner"] == job.lease_owner
        )

    @staticmethod
    def _succeed_job(connection: sqlite3.Connection, job: ClaimedJob) -> None:
        connection.execute(
            """UPDATE jobs SET status = 'SUCCEEDED', updated_at = ?, lease_owner = NULL,
                      lease_expires_at = NULL WHERE id = ? AND lease_owner = ?""",
            (_utc_now(), job.id, job.lease_owner),
        )

    @staticmethod
    def _cancel_job(connection: sqlite3.Connection, job: ClaimedJob, reason: str) -> None:
        connection.execute(
            """UPDATE jobs SET status = 'CANCELLED', last_error = ?, updated_at = ?,
                      lease_owner = NULL, lease_expires_at = NULL
               WHERE id = ? AND lease_owner = ?""",
            (reason, _utc_now(), job.id, job.lease_owner),
        )

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        now = _utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO jobs
               (id, kind, idempotency_key, payload_json, status, available_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (_new_id(), kind, idempotency_key, _canonical_json(payload), time.time(), now, now),
        )

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        kind: str,
        external_feature_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        now = _utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO outbox_messages
               (id, kind, external_feature_id, idempotency_key, payload_json,
                status, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                _new_id(),
                kind,
                external_feature_id,
                idempotency_key,
                _canonical_json(payload),
                time.time(),
                now,
                now,
            ),
        )

    @staticmethod
    def _insert_domain_event(
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
                _new_id(),
                feature_id,
                event_type,
                actor_type,
                actor_id,
                subject_type,
                subject_id,
                _canonical_json(payload),
                _utc_now(),
            ),
        )


class ExecutionCommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(f"{result.name} failed with exit code {result.exit_code}")
        self.result = result


class NoImplementationChangesError(RuntimeError):
    pass


class RemoteHeadMismatchError(RuntimeError):
    pass


def upsert_project(database: Database, project: ProjectConfig) -> None:
    commands = [
        {"name": command.name, "argv": list(command.argv)} for command in project.commands
    ]
    now = _utc_now()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO projects
               (id, external_id, name, repository_url, default_branch, branch_prefix,
                auto_push, commands_json, created_at, updated_at, cleanup_on_done)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 external_id = excluded.external_id,
                 name = excluded.name,
                 repository_url = excluded.repository_url,
                 default_branch = excluded.default_branch,
                 branch_prefix = excluded.branch_prefix,
                 auto_push = excluded.auto_push,
                 commands_json = excluded.commands_json,
                 cleanup_on_done = excluded.cleanup_on_done,
                 updated_at = excluded.updated_at""",
            (
                project.id,
                project.external_id,
                project.name,
                project.repository_url,
                project.default_branch,
                project.branch_prefix,
                int(project.auto_push),
                _canonical_json(commands),
                now,
                now,
                int(project.cleanup_on_done),
            ),
        )
        # V0 has one TaskManagerAdapter/project binding per process. This safely
        # adopts pre-migration features and does not overwrite explicit bindings.
        connection.execute(
            "UPDATE features SET project_id = ? WHERE project_id IS NULL", (project.id,)
        )


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

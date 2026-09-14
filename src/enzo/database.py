from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN (
        'IDEA', 'INTENT', 'SPEC', 'PLAN', 'IMPLEMENTATION', 'VERIFICATION', 'DONE'
    )),
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    type TEXT NOT NULL CHECK (type IN ('INTENT')),
    display_name TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(feature_id, type)
);

CREATE TABLE IF NOT EXISTS artifact_revisions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    parent_revision_id TEXT REFERENCES artifact_revisions(id),
    status TEXT NOT NULL CHECK (status IN (
        'GENERATING', 'REVIEW', 'CHANGES_REQUESTED', 'UPDATING', 'APPROVED', 'FAILED'
    )),
    content_path TEXT,
    manifest_path TEXT,
    content_sha256 TEXT,
    created_by_kind TEXT NOT NULL,
    created_by_id TEXT NOT NULL,
    agent_run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, revision_no)
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    artifact_revision_id TEXT NOT NULL REFERENCES artifact_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'CHANGES_REQUESTED')),
    reviewer_external_id TEXT,
    source_event_id TEXT,
    opened_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_review_per_revision
ON reviews(artifact_revision_id) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS feedback_items (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    artifact_revision_id TEXT NOT NULL REFERENCES artifact_revisions(id),
    source_comment_id TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL,
    section TEXT,
    comment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'DISMISSED')),
    resolution_type TEXT CHECK (resolution_type IN ('MANUAL', 'AGENT')),
    resolved_by TEXT,
    resolved_in_revision_id TEXT REFERENCES artifact_revisions(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(source_comment_id, source_ordinal)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    artifact_revision_id TEXT NOT NULL REFERENCES artifact_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SUPERSEDED')),
    request_json TEXT NOT NULL,
    output_sha256 TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS inbox_events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    delivery_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'PROCESSED', 'REJECTED')),
    outcome TEXT,
    message TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS jobs_claimable
ON jobs(status, available_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS outbox_messages (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    external_feature_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'SENT')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    external_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS outbox_claimable
ON outbox_messages(status, available_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS domain_events (
    id TEXT PRIMARY KEY,
    feature_id TEXT REFERENCES features(id),
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""

EXPAND_ARTIFACT_TYPES = """
PRAGMA foreign_keys = OFF;

CREATE TABLE artifacts_v2 (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    type TEXT NOT NULL CHECK (type IN ('INTENT', 'SPEC')),
    display_name TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(feature_id, type)
);

INSERT INTO artifacts_v2
SELECT id, feature_id, type, display_name, current_revision_id, created_at
FROM artifacts;

DROP TABLE artifacts;
ALTER TABLE artifacts_v2 RENAME TO artifacts;

-- V0.1 queued GENERATE_SPEC before a SPEC artifact/revision existed. Those
-- legacy jobs cannot satisfy the versioned-artifact contract and are fenced
-- off during upgrade. New GENERATE_SPEC jobs always carry a revision_id.
UPDATE jobs
SET status = 'CANCELLED',
    last_error = 'superseded by versioned SPEC artifact migration',
    updated_at = CURRENT_TIMESTAMP,
    lease_owner = NULL,
    lease_expires_at = NULL
WHERE kind = 'GENERATE_SPEC'
  AND status IN ('PENDING', 'RUNNING')
  AND payload_json NOT LIKE '%"revision_id"%';

PRAGMA foreign_keys = ON;
"""


ADD_PLAN_ARTIFACT_AND_EXECUTION_BREAKDOWN = """
PRAGMA foreign_keys = OFF;

CREATE TABLE artifacts_v3 (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    type TEXT NOT NULL CHECK (type IN ('INTENT', 'SPEC', 'PLAN')),
    display_name TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(feature_id, type)
);

INSERT INTO artifacts_v3
SELECT id, feature_id, type, display_name, current_revision_id, created_at
FROM artifacts;

DROP TABLE artifacts;
ALTER TABLE artifacts_v3 RENAME TO artifacts;

CREATE TABLE execution_plans (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    artifact_revision_id TEXT NOT NULL UNIQUE REFERENCES artifact_revisions(id),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    plan_path TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE execution_tasks (
    id TEXT PRIMARY KEY,
    execution_plan_id TEXT NOT NULL REFERENCES execution_plans(id),
    task_key TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    verification_refs_json TEXT NOT NULL,
    UNIQUE(execution_plan_id, task_key),
    UNIQUE(execution_plan_id, position)
);

-- V0.2 used a placeholder GENERATE_PLAN job with no versioned PLAN revision.
UPDATE jobs
SET status = 'CANCELLED',
    last_error = 'superseded by versioned PLAN artifact migration',
    updated_at = CURRENT_TIMESTAMP,
    lease_owner = NULL,
    lease_expires_at = NULL
WHERE kind = 'GENERATE_PLAN'
  AND status IN ('PENDING', 'RUNNING')
  AND payload_json NOT LIKE '%"revision_id"%';

PRAGMA foreign_keys = ON;
"""


ADD_PROJECT_REPOSITORY_AND_EXECUTIONS = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    repository_url TEXT,
    default_branch TEXT NOT NULL,
    branch_prefix TEXT NOT NULL,
    auto_push INTEGER NOT NULL CHECK (auto_push IN (0, 1)),
    commands_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE features ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX features_by_project ON features(project_id, created_at);

CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    execution_plan_id TEXT NOT NULL REFERENCES execution_plans(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'CLOSED'
    )),
    base_sha TEXT,
    branch TEXT,
    worktree_path TEXT,
    head_sha TEXT,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    agent TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(feature_id, execution_plan_id, attempt)
);

CREATE INDEX executions_by_feature ON executions(feature_id, created_at);

CREATE TABLE verification_evidence (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(id),
    evidence_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    command_json TEXT,
    exit_code INTEGER,
    duration_seconds REAL,
    log_path TEXT NOT NULL,
    log_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(execution_id, name)
);
"""


ADD_IMPLEMENTATION_REVIEW_CYCLE = """
ALTER TABLE projects
ADD COLUMN cleanup_on_done INTEGER NOT NULL DEFAULT 1 CHECK (cleanup_on_done IN (0, 1));

CREATE TABLE implementation_revisions (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    parent_revision_id TEXT REFERENCES implementation_revisions(id),
    status TEXT NOT NULL CHECK (status IN (
        'UPDATING', 'REVIEW', 'CHANGES_REQUESTED', 'APPROVED', 'FAILED'
    )),
    base_sha TEXT,
    head_sha TEXT,
    diff_path TEXT,
    diff_sha256 TEXT,
    created_by_kind TEXT NOT NULL,
    created_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(execution_id, revision_no)
);

ALTER TABLE executions
ADD COLUMN current_revision_id TEXT REFERENCES implementation_revisions(id);

CREATE TABLE implementation_reviews (
    id TEXT PRIMARY KEY,
    implementation_revision_id TEXT NOT NULL REFERENCES implementation_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'CHANGES_REQUESTED')),
    reviewer_external_id TEXT,
    source_event_id TEXT,
    opened_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE UNIQUE INDEX one_pending_implementation_review_per_revision
ON implementation_reviews(implementation_revision_id) WHERE status = 'PENDING';

CREATE TABLE implementation_feedback_items (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES implementation_reviews(id),
    implementation_revision_id TEXT NOT NULL REFERENCES implementation_revisions(id),
    source_comment_id TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL,
    section TEXT,
    comment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'DISMISSED')),
    resolution_type TEXT CHECK (resolution_type IN ('MANUAL', 'AGENT')),
    resolved_by TEXT,
    resolved_in_revision_id TEXT REFERENCES implementation_revisions(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(source_comment_id, source_ordinal)
);

-- Give executions created by the previous walking slice a first reviewable
-- implementation revision. Failed/running rows remain inspectable but do not
-- cross a human gate.
INSERT INTO implementation_revisions
    (id, execution_id, revision_no, status, base_sha, head_sha,
     created_by_kind, created_by_id, created_at)
SELECT 'migration-r1-' || id,
       id,
       1,
       CASE status
         WHEN 'SUCCEEDED' THEN 'REVIEW'
         WHEN 'CLOSED' THEN 'APPROVED'
         WHEN 'FAILED' THEN 'FAILED'
         ELSE 'UPDATING'
       END,
       base_sha,
       head_sha,
       'SYSTEM',
       'migration-5',
       created_at
FROM executions;

UPDATE executions
SET current_revision_id = 'migration-r1-' || id;

INSERT INTO implementation_reviews
    (id, implementation_revision_id, status, opened_at)
SELECT 'migration-review-' || id,
       'migration-r1-' || id,
       'PENDING',
       COALESCE(finished_at, updated_at)
FROM executions
WHERE status = 'SUCCEEDED';

ALTER TABLE verification_evidence RENAME TO verification_evidence_v4;

CREATE TABLE verification_evidence (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(id),
    implementation_revision_id TEXT REFERENCES implementation_revisions(id),
    evidence_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    command_json TEXT,
    exit_code INTEGER,
    duration_seconds REAL,
    log_path TEXT NOT NULL,
    log_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO verification_evidence
    (id, execution_id, implementation_revision_id, evidence_type, name, status,
     command_json, exit_code, duration_seconds, log_path, log_sha256, created_at)
SELECT id,
       execution_id,
       'migration-r1-' || execution_id,
       evidence_type,
       name,
       status,
       command_json,
       exit_code,
       duration_seconds,
       log_path,
       log_sha256,
       created_at
FROM verification_evidence_v4;

DROP TABLE verification_evidence_v4;

CREATE UNIQUE INDEX one_execution_level_evidence_name
ON verification_evidence(execution_id, name)
WHERE implementation_revision_id IS NULL;

CREATE UNIQUE INDEX one_revision_evidence_name
ON verification_evidence(implementation_revision_id, name)
WHERE implementation_revision_id IS NOT NULL;

UPDATE features
SET stage = 'VERIFICATION', version = version + 1, updated_at = CURRENT_TIMESTAMP
WHERE stage = 'IMPLEMENTATION'
  AND id IN (SELECT feature_id FROM executions WHERE status = 'SUCCEEDED');

INSERT OR IGNORE INTO outbox_messages
    (id, kind, external_feature_id, idempotency_key, payload_json,
     status, available_at, created_at, updated_at)
SELECT 'migration-stage-' || e.id,
       'STAGE_UPDATED',
       f.external_id,
       'execution:' || e.id || ':stage:verification:migration-5',
       json_object('stage', 'VERIFICATION', 'attention', 'enzo:needs-review'),
       'PENDING',
       unixepoch('now'),
       CURRENT_TIMESTAMP,
       CURRENT_TIMESTAMP
FROM executions e
JOIN features f ON f.id = e.feature_id
WHERE e.status = 'SUCCEEDED';

INSERT OR IGNORE INTO outbox_messages
    (id, kind, external_feature_id, idempotency_key, payload_json,
     status, available_at, created_at, updated_at)
SELECT 'migration-review-notice-' || e.id,
       'IMPLEMENTATION_REVIEW_REQUIRED',
       f.external_id,
       'implementation:migration-r1-' || e.id || ':review-required',
       json_object(
         'execution_id', e.id,
         'implementation_revision_id', 'migration-r1-' || e.id,
         'revision', 1,
         'branch', e.branch,
         'base_sha', e.base_sha,
         'head_sha', e.head_sha,
         'diff_stat', 'See recorded execution evidence',
         'attention', 'enzo:needs-review'
       ),
       'PENDING',
       unixepoch('now'),
       CURRENT_TIMESTAMP,
       CURRENT_TIMESTAMP
FROM executions e
JOIN features f ON f.id = e.feature_id
WHERE e.status = 'SUCCEEDED';
"""


ADD_AGENT_RUNNER_PROVENANCE = """
ALTER TABLE agent_runs ADD COLUMN workload TEXT;
ALTER TABLE agent_runs ADD COLUMN runner_name TEXT;
ALTER TABLE agent_runs ADD COLUMN runner_kind TEXT;
ALTER TABLE agent_runs ADD COLUMN provider TEXT;
ALTER TABLE agent_runs ADD COLUMN model TEXT;
ALTER TABLE agent_runs ADD COLUMN result_json TEXT;
ALTER TABLE agent_runs ADD COLUMN failure_kind TEXT;
ALTER TABLE agent_runs ADD COLUMN retryable INTEGER CHECK (retryable IN (0, 1));
"""


ADD_ARTIFACT_FEEDBACK_LOCATIONS = """
ALTER TABLE feedback_items ADD COLUMN location_json TEXT;

CREATE INDEX feedback_by_revision_status
ON feedback_items(artifact_revision_id, status, source_ordinal);
"""


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA),
    (2, EXPAND_ARTIFACT_TYPES),
    (3, ADD_PLAN_ARTIFACT_AND_EXECUTION_BREAKDOWN),
    (4, ADD_PROJECT_REPOSITORY_AND_EXECUTIONS),
    (5, ADD_IMPLEMENTATION_REVIEW_CYCLE),
    (6, ADD_AGENT_RUNNER_PROVENANCE),
    (7, ADD_ARTIFACT_FEEDBACK_LOCATIONS),
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import enzo.database as database_module
from enzo.database import (
    ADD_PLAN_ARTIFACT_AND_EXECUTION_BREAKDOWN,
    ADD_PROJECT_REPOSITORY_AND_EXECUTIONS,
    EXPAND_ARTIFACT_TYPES,
    SCHEMA,
    Database,
    DatabaseMigrationError,
)


def test_v1_database_expands_artifact_types_and_fences_legacy_jobs(tmp_path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 'now')"
    )
    connection.execute(
        """INSERT INTO jobs
           (id, kind, idempotency_key, payload_json, status, available_at, created_at, updated_at)
           VALUES ('legacy', 'GENERATE_SPEC', 'legacy-spec',
                   '{"feature_id":"feature-1"}', 'PENDING', ?, 'now', 'now')""",
        (time.time(),),
    )
    connection.execute(
        """INSERT INTO jobs
           (id, kind, idempotency_key, payload_json, status, available_at, created_at, updated_at)
           VALUES ('legacy-plan', 'GENERATE_PLAN', 'legacy-plan',
                   '{"feature_id":"feature-1"}', 'PENDING', ?, 'now', 'now')""",
        (time.time(),),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()

    with database.read() as upgraded:
        artifact_sql = upgraded.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
        ).fetchone()["sql"]
        assert "'SPEC'" in artifact_sql
        assert "'PLAN'" in artifact_sql
        legacy = upgraded.execute(
            "SELECT status, last_error FROM jobs WHERE id = 'legacy'"
        ).fetchone()
        assert legacy["status"] == "CANCELLED"
        assert "versioned SPEC artifact" in legacy["last_error"]
        legacy_plan = upgraded.execute(
            "SELECT status, last_error FROM jobs WHERE id = 'legacy-plan'"
        ).fetchone()
        assert legacy_plan["status"] == "CANCELLED"
        assert "versioned PLAN artifact" in legacy_plan["last_error"]
        assert upgraded.execute(
            "SELECT count(*) FROM schema_migrations WHERE version IN (2, 3, 4, 5, 6)"
        ).fetchone()[0] == 5
        assert upgraded.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 7"
        ).fetchone()[0] == 1
        feedback_columns = {
            row["name"] for row in upgraded.execute("PRAGMA table_info(feedback_items)")
        }
        assert "location_json" in feedback_columns
        assert upgraded.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'execution_tasks'"
        ).fetchone()[0] == 1


def test_v4_succeeded_execution_is_migrated_to_an_explicit_review_gate(tmp_path) -> None:
    path = tmp_path / "v4.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    for version, script in (
        (1, SCHEMA),
        (2, EXPAND_ARTIFACT_TYPES),
        (3, ADD_PLAN_ARTIFACT_AND_EXECUTION_BREAKDOWN),
        (4, ADD_PROJECT_REPOSITORY_AND_EXECUTIONS),
    ):
        connection.executescript(script)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'now')",
            (version,),
        )
    connection.execute(
        """INSERT INTO projects
           (id, external_id, name, repository_url, default_branch, branch_prefix,
            auto_push, commands_json, created_at, updated_at)
           VALUES ('project-1', 'plane-project', 'Project', '/remote', 'main',
                   'ai/', 1, '[]', 'now', 'now')"""
    )
    connection.execute(
        """INSERT INTO features
           (id, external_id, title, stage, version, created_at, updated_at, project_id)
           VALUES ('feature-1', 'PLANE-1', 'Feature', 'IMPLEMENTATION', 7,
                   'now', 'now', 'project-1')"""
    )
    connection.execute(
        """INSERT INTO artifacts
           (id, feature_id, type, display_name, current_revision_id, created_at)
           VALUES ('plan-1', 'feature-1', 'PLAN', 'plan.md', 'plan-revision-1', 'now')"""
    )
    connection.execute(
        """INSERT INTO artifact_revisions
           (id, artifact_id, revision_no, status, content_path,
            created_by_kind, created_by_id, created_at)
           VALUES ('plan-revision-1', 'plan-1', 1, 'APPROVED', '/plan.md',
                   'AGENT', 'fake', 'now')"""
    )
    connection.execute(
        """INSERT INTO execution_plans
           (id, feature_id, artifact_revision_id, schema_version,
            plan_path, plan_sha256, created_at)
           VALUES ('execution-plan-1', 'feature-1', 'plan-revision-1', 1,
                   '/plan.yaml', 'plan-sha', 'now')"""
    )
    connection.execute(
        """INSERT INTO executions
           (id, feature_id, execution_plan_id, project_id, status, base_sha,
            branch, worktree_path, head_sha, attempt, agent, started_at,
            finished_at, result_json, created_at, updated_at)
           VALUES ('execution-1', 'feature-1', 'execution-plan-1', 'project-1',
                   'SUCCEEDED', 'base', 'ai/plane-1', '/worktree', 'head', 1,
                   'FakeAgentRunner', 'now', 'now', '{}', 'now', 'now')"""
    )
    connection.execute(
        """INSERT INTO verification_evidence
           (id, execution_id, evidence_type, name, status, command_json,
            exit_code, duration_seconds, log_path, log_sha256, created_at)
           VALUES ('evidence-1', 'execution-1', 'TEST', 'test', 'PASS', '[]',
                   0, 0.1, '/test.log', 'evidence-sha', 'now')"""
    )
    connection.commit()
    connection.close()

    Database(path).initialize()

    with Database(path).read() as upgraded:
        feature = upgraded.execute(
            "SELECT stage, version FROM features WHERE id = 'feature-1'"
        ).fetchone()
        assert (feature["stage"], feature["version"]) == ("VERIFICATION", 8)
        revision = upgraded.execute(
            "SELECT revision_no, status FROM implementation_revisions"
        ).fetchone()
        assert (revision["revision_no"], revision["status"]) == (1, "REVIEW")
        assert upgraded.execute(
            "SELECT status FROM implementation_reviews"
        ).fetchone()["status"] == "PENDING"
        assert upgraded.execute(
            "SELECT implementation_revision_id FROM verification_evidence"
        ).fetchone()["implementation_revision_id"] == "migration-r1-execution-1"
        assert upgraded.execute(
            """SELECT count(*) FROM outbox_messages
               WHERE kind IN ('STAGE_UPDATED', 'IMPLEMENTATION_REVIEW_REQUIRED')
                 AND status = 'PENDING'"""
        ).fetchone()[0] == 2
        assert upgraded.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone()[0] == 1
        assert upgraded.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'executions'"
        ).fetchone()[0] == 1
        assert upgraded.execute(
            """SELECT count(*) FROM sqlite_master
               WHERE type = 'table' AND name = 'implementation_revisions'"""
        ).fetchone()[0] == 1


def test_recorded_partial_agent_provenance_migration_is_repaired(tmp_path) -> None:
    path = tmp_path / "partial-v6.db"
    database = Database(path)
    database.initialize()

    with database.transaction() as connection:
        connection.execute("ALTER TABLE agent_runs DROP COLUMN result_json")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN failure_kind")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN retryable")

    database.initialize()

    with database.read() as repaired:
        columns = {
            row["name"] for row in repaired.execute("PRAGMA table_info(agent_runs)")
        }
        assert {"result_json", "failure_kind", "retryable"} <= columns
        assert repaired.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0] == 1


def test_failed_migration_rolls_back_schema_and_ledger(tmp_path, monkeypatch) -> None:
    path = tmp_path / "failed.db"
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        ((99, "CREATE TABLE partial_table (id TEXT);\nTHIS IS NOT SQL;"),),
    )

    with pytest.raises(DatabaseMigrationError, match="migration 99 failed"):
        Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'partial_table'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 99"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_recorded_but_incomplete_non_additive_migration_fails_at_startup(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "incomplete.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 'now')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", ((1, SCHEMA),))

    with pytest.raises(DatabaseMigrationError, match="required tables are missing"):
        Database(path).initialize()


def test_concurrent_initializers_apply_each_migration_once(tmp_path) -> None:
    path = tmp_path / "concurrent.db"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Database(path).initialize(), range(2)))

    with Database(path).read() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7]

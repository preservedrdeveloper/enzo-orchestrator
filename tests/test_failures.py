from __future__ import annotations

import pytest
from conftest import Harness
from test_spec_cycle import create_ready_spec

from enzo.artifacts.definitions import InvalidArtifactError
from enzo.artifacts.execution_plan import InvalidExecutionPlanError
from enzo.domain import AgentResult, AgentRole


def test_invalid_agent_output_fails_revision_without_crossing_review_gate(harness: Harness) -> None:
    harness.agent.enqueue(AgentRole.INTENT, AgentResult(content="# Intent\n\nIncomplete"))
    feature = harness.plane.create_feature("Invalid agent output", "PLANE-INVALID")
    harness.orchestrator.ingest_feature(feature, delivery_id="invalid:create")

    with pytest.raises(InvalidArtifactError):
        harness.orchestrator.run_one_job(worker_id="failure-worker")

    snapshot = harness.orchestrator.feature_snapshot("PLANE-INVALID")
    assert snapshot["stage"] == "INTENT"
    assert snapshot["artifact"]["artifact_status"] == "FAILED"
    with harness.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM jobs").fetchone()["status"] == "FAILED"
        assert connection.execute("SELECT status FROM agent_runs").fetchone()["status"] == "FAILED"


def test_invalid_execution_dag_fails_plan_before_human_review(harness: Harness) -> None:
    external_id = create_ready_spec(harness, external_id="PLANE-INVALID-PLAN")
    harness.agent.enqueue(
        AgentRole.PLAN,
        AgentResult(
            content="""# Implementation Plan

## Summary
Summary.
## Relevant Existing Implementation
Inspect it.
## Components / Modules Affected
Resolve them.
## Implementation Steps
Implement it.
## File-Level Changes
Resolve paths.
## Testing Plan
Run tests.
## Verification Mapping
Map evidence.
## Risks
None.
## Execution Breakdown
Two cyclic tasks.
""",
            metadata={
                "execution_plan": {
                    "schema_version": 1,
                    "tasks": [
                        {
                            "key": "a",
                            "title": "A",
                            "description": "A",
                            "depends_on": ["b"],
                            "verification_refs": [],
                        },
                        {
                            "key": "b",
                            "title": "B",
                            "description": "B",
                            "depends_on": ["a"],
                            "verification_refs": [],
                        },
                    ],
                }
            },
        ),
    )
    approve = harness.plane.add_comment(
        external_id,
        "reviewer-1",
        "@enzo approve spec@1",
        "invalid-plan:approve-spec",
    )
    harness.orchestrator.ingest_comment(approve)

    with pytest.raises(InvalidExecutionPlanError, match="contain a cycle"):
        harness.orchestrator.run_one_job(worker_id="failure-worker")

    snapshot = harness.orchestrator.feature_snapshot(external_id)
    assert snapshot["stage"] == "PLAN"
    assert snapshot["artifact"]["artifact_status"] == "FAILED"
    with harness.database.read() as connection:
        assert connection.execute(
            """SELECT count(*) FROM reviews r
               JOIN artifact_revisions ar ON ar.id = r.artifact_revision_id
               JOIN artifacts a ON a.id = ar.artifact_id
               WHERE a.type = 'PLAN'"""
        ).fetchone()[0] == 0

from __future__ import annotations

import pytest

from enzo.artifacts.execution_plan import (
    InvalidExecutionPlanError,
    execution_plan_yaml,
    normalize_execution_plan,
)


def task(key: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "key": key,
        "title": key.replace("-", " ").title(),
        "description": f"Execute {key}",
        "depends_on": depends_on or [],
        "verification_refs": ["VC-1"],
    }


def test_execution_plan_normalizes_and_serializes_dependencies() -> None:
    plan = normalize_execution_plan(
        {
            "schema_version": 1,
            "tasks": [task("domain"), task("backend", depends_on=["domain"])],
        }
    )

    rendered = execution_plan_yaml(plan)

    assert 'key: "domain"' in rendered
    assert 'depends_on: []' in rendered
    assert '- "domain"' in rendered


@pytest.mark.parametrize(
    "tasks,error",
    [
        ([task("backend", depends_on=["missing"])], "unknown tasks"),
        (
            [task("a", depends_on=["b"]), task("b", depends_on=["a"])],
            "contain a cycle",
        ),
        ([task("duplicate"), task("duplicate")], "duplicate execution task key"),
    ],
)
def test_execution_plan_rejects_invalid_dependency_graph(tasks: list[dict], error: str) -> None:
    with pytest.raises(InvalidExecutionPlanError, match=error):
        normalize_execution_plan({"schema_version": 1, "tasks": tasks})

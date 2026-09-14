from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TASK_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class InvalidExecutionPlanError(ValueError):
    pass


def normalize_execution_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidExecutionPlanError("execution plan must be an object")
    if value.get("schema_version") != 1:
        raise InvalidExecutionPlanError("execution plan schema_version must be 1")
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise InvalidExecutionPlanError("execution plan must contain at least one task")
    if len(raw_tasks) > 100:
        raise InvalidExecutionPlanError("execution plan cannot contain more than 100 tasks")

    tasks: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise InvalidExecutionPlanError("each execution task must be an object")
        key = raw.get("key")
        title = raw.get("title")
        description = raw.get("description")
        depends_on = raw.get("depends_on", [])
        verification_refs = raw.get("verification_refs", [])
        if not isinstance(key, str) or not TASK_KEY_RE.fullmatch(key):
            raise InvalidExecutionPlanError(f"invalid execution task key: {key!r}")
        if key in keys:
            raise InvalidExecutionPlanError(f"duplicate execution task key: {key}")
        if not isinstance(title, str) or not title.strip():
            raise InvalidExecutionPlanError(f"task {key} must have a title")
        if not isinstance(description, str) or not description.strip():
            raise InvalidExecutionPlanError(f"task {key} must have a description")
        if not isinstance(depends_on, list) or not all(
            isinstance(item, str) for item in depends_on
        ):
            raise InvalidExecutionPlanError(f"task {key} depends_on must be a string list")
        if len(depends_on) != len(set(depends_on)) or key in depends_on:
            raise InvalidExecutionPlanError(f"task {key} has invalid duplicate/self dependencies")
        if not isinstance(verification_refs, list) or not all(
            isinstance(item, str) and item.strip() for item in verification_refs
        ):
            raise InvalidExecutionPlanError(
                f"task {key} verification_refs must be a string list"
            )
        keys.add(key)
        tasks.append(
            {
                "key": key,
                "title": title.strip(),
                "description": description.strip(),
                "depends_on": list(depends_on),
                "verification_refs": list(verification_refs),
            }
        )

    for task in tasks:
        unknown = set(task["depends_on"]) - keys
        if unknown:
            raise InvalidExecutionPlanError(
                f"task {task['key']} depends on unknown tasks: {', '.join(sorted(unknown))}"
            )
    _assert_acyclic(tasks)
    return {"schema_version": 1, "tasks": tasks}


def _assert_acyclic(tasks: list[dict[str, Any]]) -> None:
    dependencies = {task["key"]: set(task["depends_on"]) for task in tasks}
    remaining = set(dependencies)
    while remaining:
        ready = {key for key in remaining if not (dependencies[key] & remaining)}
        if not ready:
            raise InvalidExecutionPlanError("execution task dependencies contain a cycle")
        remaining -= ready


def execution_plan_yaml(plan: dict[str, Any]) -> str:
    normalized = normalize_execution_plan(plan)
    lines = ["schema_version: 1", "tasks:"]
    for task in normalized["tasks"]:
        lines.extend(
            [
                f"  - key: {json.dumps(task['key'])}",
                f"    title: {json.dumps(task['title'])}",
                f"    description: {json.dumps(task['description'])}",
                "    depends_on:",
            ]
        )
        if task["depends_on"]:
            lines.extend(f"      - {json.dumps(item)}" for item in task["depends_on"])
        else:
            lines[-1] = "    depends_on: []"
        lines.append("    verification_refs:")
        if task["verification_refs"]:
            lines.extend(
                f"      - {json.dumps(item)}" for item in task["verification_refs"]
            )
        else:
            lines[-1] = "    verification_refs: []"
    return "\n".join(lines) + "\n"


def execution_plan_sha256(plan_yaml: str) -> str:
    return hashlib.sha256(plan_yaml.encode("utf-8")).hexdigest()

from __future__ import annotations

import threading
import uuid
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path

from .domain import (
    AgentCapability,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRunInfo,
    AgentRunnerSpec,
    ExternalComment,
    ExternalFeature,
    OutboxMessage,
)


class FakeAgentRunner:
    """Deterministic agent for orchestration tests and the V0 demo."""

    def __init__(self) -> None:
        self._scripted: dict[AgentRole, deque[AgentResult | Exception]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.requests: list[AgentRequest] = []

    @property
    def spec(self) -> AgentRunnerSpec:
        return AgentRunnerSpec(
            name="fake",
            kind="fake",
            capabilities=frozenset(
                {AgentCapability.GENERATE_TEXT, AgentCapability.EDIT_WORKSPACE}
            ),
        )

    def enqueue(self, role: AgentRole, result: AgentResult | Exception) -> None:
        with self._lock:
            self._scripted[role].append(result)

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def run(self, request: AgentRequest) -> AgentResult:
        with self._lock:
            self.requests.append(request)
            scripted = self._scripted[request.role].popleft() if self._scripted[request.role] else None
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            result = scripted
        elif request.role is AgentRole.INTENT:
            result = AgentResult(content=_default_intent(request.title))
        elif request.role is AgentRole.SPEC:
            result = AgentResult(content=_default_spec(request))
        elif request.role is AgentRole.PLAN:
            result = _default_plan(request)
        elif request.role is AgentRole.CODER:
            result = _fake_implementation(request)
        elif (
            request.role is AgentRole.FEEDBACK_RESOLVER
            and request.metadata.get("target_kind") == "IMPLEMENTATION"
        ):
            result = _revise_fake_implementation(request)
        elif request.metadata.get("artifact_type") == "SPEC":
            result = AgentResult(content=_revised_spec(request))
        elif request.metadata.get("artifact_type") == "PLAN":
            result = _revised_plan(request)
        else:
            result = AgentResult(content=_revised_intent(request))
        return replace(
            result,
            run_info=AgentRunInfo(runner_name=self.spec.name, runner_kind=self.spec.kind),
        )


class BlockingFakeAgent(FakeAgentRunner):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered = entered
        self.release = release

    def run(self, request: AgentRequest) -> AgentResult:
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release blocking fake agent")
        return super().run(request)


def _default_intent(title: str) -> str:
    return f"""# Intent

## Problem

The current workflow does not yet address: {title}.

## User / Actor

The primary user described by the feature request.

## Desired Outcome

The user can achieve the requested outcome with clear, observable behavior.

## Scope

- Clarify the requested product behavior.
- Define acceptance signals before implementation.

## Out of Scope

- Implementation details.
- Unrelated product changes.

## Constraints

- Preserve existing behavior outside the stated scope.

## Acceptance Signals

- A reviewer agrees that the problem and desired outcome are unambiguous.

## Open Questions

- Are there additional actors or constraints?
"""


def _revised_intent(request: AgentRequest) -> str:
    feedback_lines = "\n".join(
        f"- {f'[{item.section}] ' if item.section else ''}{item.comment}"
        for item in request.unresolved_feedback
    )
    return f"""# Intent

## Problem

The feature \"{request.title}\" needs a reviewed, explicit product intent.

## User / Actor

The primary user and every actor named in the reviewed feedback.

## Desired Outcome

The requested outcome incorporates all explicit unresolved review feedback.

## Scope

- The behavior described by this feature.
- Clarifications requested by reviewers.

## Out of Scope

- Implementation and architecture decisions.

## Constraints

- Existing behavior outside the approved scope remains unchanged.

## Acceptance Signals

- Every feedback item below is explicitly addressed.
- A human approves this revision.

## Open Questions

- None currently recorded.

## Addressed Feedback

{feedback_lines}
"""


def _default_spec(request: AgentRequest) -> str:
    approved_intent = request.metadata.get("previous_artifacts", {}).get("intent.md", "")
    intent_summary = next(
        (line.strip() for line in approved_intent.splitlines() if line.strip() and not line.startswith("#")),
        request.title,
    )
    return f"""# Specification

## Approved Intent

Feature: {request.title}

Intent basis: {intent_summary}

## Functional Requirements

- The feature exposes the reviewed behavior described by the approved intent.
- Existing behavior outside the approved scope remains unchanged.
- User-visible state is deterministic and survives the interactions required by the intent.

## User Experience

- Reuse existing interaction patterns where repository context provides them.
- Present clear selected, empty, loading, and error states.

## Edge Cases

- Empty results and unavailable dependencies are handled explicitly.
- Repeated actions do not create duplicate effects.

## Existing System Context

- Inspect the repository before choosing components, APIs, or data structures.

## Architecture / Design

- Prefer the smallest change that satisfies the approved intent and verification contract.

## Security / Permissions

- Preserve existing authorization boundaries and avoid exposing restricted data.

## Risks

- Existing system constraints may require refinement after repository inspection.

## Verification Contract

### Functional

- The primary desired outcome is covered by an automated test.
- Out-of-scope behavior remains unchanged.

### Deterministic Evidence

- Configured lint passes.
- Configured tests pass.
- Configured build passes.
"""


def _revised_spec(request: AgentRequest) -> str:
    feedback_lines = "\n".join(
        f"- {f'[{item.section}] ' if item.section else ''}{item.comment}"
        for item in request.unresolved_feedback
    )
    previous = request.current_content or _default_spec(request)
    return f"""{previous.rstrip()}

## Addressed Feedback

{feedback_lines}
"""


def _default_plan(request: AgentRequest) -> AgentResult:
    execution_plan = {
        "schema_version": 1,
        "tasks": [
            {
                "key": "inspect-context",
                "title": "Inspect the relevant existing implementation",
                "description": "Locate the existing modules, conventions, and verification commands before editing.",
                "depends_on": [],
                "verification_refs": [],
            },
            {
                "key": "implement-feature",
                "title": "Implement the approved specification",
                "description": "Make the smallest code and data changes required by the approved specification.",
                "depends_on": ["inspect-context"],
                "verification_refs": ["VC-FUNCTIONAL-1"],
            },
            {
                "key": "verify-feature",
                "title": "Produce deterministic verification evidence",
                "description": "Run configured lint, tests, and build plus the mapped feature assertions.",
                "depends_on": ["implement-feature"],
                "verification_refs": ["VC-FUNCTIONAL-1", "VC-DETERMINISTIC-1"],
            },
        ],
    }
    content = f"""# Implementation Plan

## Summary

Implement the approved specification for {request.title} with a small, reviewable change set.

## Relevant Existing Implementation

- Inspect the repository for the current feature path and established patterns before editing.

## Components / Modules Affected

- Exact modules are resolved from repository context during execution.

## Implementation Steps

1. Inspect the relevant implementation and tests.
2. Implement the approved functional and interaction behavior.
3. Run every mapped verification requirement.

## File-Level Changes

- Record exact file changes after repository inspection; do not invent paths before inspection.

## Dependencies

- Use existing project dependencies unless the approved specification requires otherwise.

## Testing Plan

- Add or update automated coverage for the functional behavior.
- Run configured lint, test, and build commands.

## Verification Mapping

- Approved functional behavior → `VC-FUNCTIONAL-1` → automated test.
- Deterministic project checks → `VC-DETERMINISTIC-1` → lint, test, and build logs.

## Risks

- Repository inspection may reveal compatibility constraints that require a reviewed Plan revision.

## Execution Breakdown

1. `inspect-context`
2. `implement-feature` (depends on `inspect-context`)
3. `verify-feature` (depends on `implement-feature`)
"""
    return AgentResult(content=content, metadata={"execution_plan": execution_plan})


def _revised_plan(request: AgentRequest) -> AgentResult:
    feedback_lines = "\n".join(
        f"- {f'[{item.section}] ' if item.section else ''}{item.comment}"
        for item in request.unresolved_feedback
    )
    execution_plan = request.metadata.get("current_execution_plan")
    if not execution_plan:
        execution_plan = _default_plan(request).metadata["execution_plan"]
    content = f"""{(request.current_content or _default_plan(request).content).rstrip()}

## Addressed Feedback

{feedback_lines}
"""
    return AgentResult(content=content, metadata={"execution_plan": execution_plan})


def _fake_implementation(request: AgentRequest) -> AgentResult:
    if not request.workspace_path:
        raise ValueError("CODER requests require a workspace_path")
    output = Path(request.workspace_path) / "enzo-implementation.txt"
    output.write_text(
        f"Fake implementation for {request.title}\n"
        f"Feature: {request.feature_id}\n"
        f"Plan revision: {request.artifact_revision_id}\n",
        encoding="utf-8",
    )
    return AgentResult(
        content="Fake coding execution completed.",
        metadata={"changed_files": [output.name]},
    )


def _revise_fake_implementation(request: AgentRequest) -> AgentResult:
    if not request.workspace_path:
        raise ValueError("implementation feedback requests require a workspace_path")
    output = Path(request.workspace_path) / "enzo-implementation.txt"
    if not output.is_file():
        raise FileNotFoundError(f"implementation file is missing: {output}")
    feedback = "\n".join(
        f"- {f'[{item.section}] ' if item.section else ''}{item.comment}"
        for item in request.unresolved_feedback
    )
    with output.open("a", encoding="utf-8") as stream:
        stream.write(
            f"\nAddressed in implementation revision "
            f"{request.metadata['implementation_revision']}:\n{feedback}\n"
        )
    return AgentResult(
        content="Fake implementation feedback resolution completed.",
        metadata={
            "changed_files": [output.name],
            "addressed_feedback_count": len(request.unresolved_feedback),
        },
    )


class FakeTaskManagerAdapter:
    """In-memory Plane-shaped control-plane projection with idempotent delivery."""

    def __init__(self) -> None:
        self.features: dict[str, ExternalFeature] = {}
        self.comments: dict[str, ExternalComment] = {}
        self.stages: dict[str, str] = {}
        self.attention: dict[str, str] = {}
        self.deliveries: dict[str, tuple[str, OutboxMessage]] = {}
        self.notifications: list[OutboxMessage] = []
        self.fail_next_deliveries = 0
        self._lock = threading.Lock()

    def create_feature(self, title: str, feature_id: str | None = None) -> ExternalFeature:
        feature = ExternalFeature(id=feature_id or str(uuid.uuid4()), title=title)
        with self._lock:
            self.features[feature.id] = feature
            self.stages[feature.id] = "IDEA"
        return feature

    def add_comment(
        self,
        feature_id: str,
        actor_id: str,
        body: str,
        comment_id: str | None = None,
    ) -> ExternalComment:
        comment = ExternalComment(
            id=comment_id or str(uuid.uuid4()),
            feature_id=feature_id,
            actor_id=actor_id,
            body=body,
        )
        with self._lock:
            self.comments[comment.id] = comment
        return comment

    def get_feature(self, external_feature_id: str) -> ExternalFeature:
        with self._lock:
            return self.features[external_feature_id]

    def deliver(self, message: OutboxMessage) -> str:
        with self._lock:
            if message.idempotency_key in self.deliveries:
                return self.deliveries[message.idempotency_key][0]
            if self.fail_next_deliveries:
                self.fail_next_deliveries -= 1
                raise ConnectionError("simulated Plane outage")
            external_id = f"fake-plane-{uuid.uuid4()}"
            self.deliveries[message.idempotency_key] = (external_id, message)
            self.notifications.append(message)
            if message.kind == "STAGE_UPDATED":
                self.stages[message.external_feature_id] = message.payload["stage"]
            if "attention" in message.payload:
                self.attention[message.external_feature_id] = message.payload["attention"]
            return external_id

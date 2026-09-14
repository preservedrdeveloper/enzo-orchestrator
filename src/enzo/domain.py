from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FeatureStage(StrEnum):
    IDEA = "IDEA"
    INTENT = "INTENT"
    SPEC = "SPEC"
    PLAN = "PLAN"
    IMPLEMENTATION = "IMPLEMENTATION"
    VERIFICATION = "VERIFICATION"
    DONE = "DONE"


class ArtifactType(StrEnum):
    INTENT = "INTENT"
    SPEC = "SPEC"
    PLAN = "PLAN"


class ArtifactStatus(StrEnum):
    GENERATING = "GENERATING"
    REVIEW = "REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    UPDATING = "UPDATING"
    APPROVED = "APPROVED"
    FAILED = "FAILED"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class FeedbackStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ResolutionType(StrEnum):
    MANUAL = "MANUAL"
    AGENT = "AGENT"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"


class AgentRole(StrEnum):
    INTENT = "INTENT"
    SPEC = "SPEC"
    PLAN = "PLAN"
    CODER = "CODER"
    FEEDBACK_RESOLVER = "FEEDBACK_RESOLVER"


class AgentWorkload(StrEnum):
    """Stable routing dimension, separate from the prompt persona in AgentRole."""

    ARTIFACT_GENERATION = "ARTIFACT_GENERATION"
    ARTIFACT_REVISION = "ARTIFACT_REVISION"
    IMPLEMENTATION = "IMPLEMENTATION"
    IMPLEMENTATION_REVISION = "IMPLEMENTATION_REVISION"


class AgentCapability(StrEnum):
    GENERATE_TEXT = "GENERATE_TEXT"
    EDIT_WORKSPACE = "EDIT_WORKSPACE"


class CommandAction(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    ADDRESS_WITH_AGENT = "ADDRESS_WITH_AGENT"


class ReviewTarget(StrEnum):
    INTENT = "INTENT"
    SPEC = "SPEC"
    PLAN = "PLAN"
    IMPLEMENTATION = "IMPLEMENTATION"


class EventOutcome(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"
    STALE = "STALE"


@dataclass(frozen=True)
class ExternalFeature:
    id: str
    title: str


@dataclass(frozen=True)
class ExternalComment:
    id: str
    feature_id: str
    actor_id: str
    body: str


@dataclass(frozen=True)
class TextSelection:
    """A revision-scoped anchor into the immutable Markdown source."""

    exact: str
    start_offset: int
    end_offset: int
    prefix: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class FeedbackDraft:
    section: str | None
    comment: str
    selection: TextSelection | None = None


@dataclass(frozen=True)
class ReviewCommand:
    action: CommandAction
    target: ReviewTarget
    revision: int
    feedback: tuple[FeedbackDraft, ...] = ()

    @property
    def artifact_type(self) -> ArtifactType:
        """Compatibility accessor for Markdown artifact review commands."""
        return ArtifactType(self.target)


@dataclass(frozen=True)
class AgentRequest:
    role: AgentRole
    workload: AgentWorkload
    project_id: str
    feature_id: str
    artifact_revision_id: str
    title: str
    workspace_path: str | None = None
    current_content: str | None = None
    unresolved_feedback: tuple[FeedbackDraft, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunnerSpec:
    """Static identity and capabilities advertised by one runner adapter."""

    name: str
    kind: str
    capabilities: frozenset[AgentCapability]


@dataclass(frozen=True)
class AgentRunInfo:
    """Auditable identity of the concrete runner used for one invocation."""

    runner_name: str
    runner_kind: str
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class AgentResult:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    run_info: AgentRunInfo | None = None


@dataclass(frozen=True)
class EventResult:
    outcome: EventOutcome
    message: str
    feature_id: str | None = None


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str


@dataclass(frozen=True)
class OutboxMessage:
    id: str
    kind: str
    external_feature_id: str
    idempotency_key: str
    payload: dict[str, Any]

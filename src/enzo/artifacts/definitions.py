from __future__ import annotations

from dataclasses import dataclass

from ..domain import AgentRole, ArtifactType, FeatureStage


@dataclass(frozen=True)
class ArtifactDefinition:
    artifact_type: ArtifactType
    stage: FeatureStage
    display_name: str
    generate_job: str
    revise_job: str
    generate_role: AgentRole
    next_stage: FeatureStage
    next_job: str
    next_artifact_type: ArtifactType | None
    required_headings: tuple[str, ...]
    previous_artifact_type: ArtifactType | None = None

    @property
    def command_name(self) -> str:
        return self.artifact_type.value.lower()


ARTIFACT_DEFINITIONS: dict[ArtifactType, ArtifactDefinition] = {
    ArtifactType.INTENT: ArtifactDefinition(
        artifact_type=ArtifactType.INTENT,
        stage=FeatureStage.INTENT,
        display_name="intent.md",
        generate_job="GENERATE_INTENT",
        revise_job="REVISE_INTENT",
        generate_role=AgentRole.INTENT,
        next_stage=FeatureStage.SPEC,
        next_job="GENERATE_SPEC",
        next_artifact_type=ArtifactType.SPEC,
        required_headings=(
            "# Intent",
            "## Problem",
            "## User / Actor",
            "## Desired Outcome",
            "## Scope",
            "## Out of Scope",
            "## Constraints",
            "## Acceptance Signals",
            "## Open Questions",
        ),
    ),
    ArtifactType.SPEC: ArtifactDefinition(
        artifact_type=ArtifactType.SPEC,
        stage=FeatureStage.SPEC,
        display_name="spec.md",
        generate_job="GENERATE_SPEC",
        revise_job="REVISE_SPEC",
        generate_role=AgentRole.SPEC,
        next_stage=FeatureStage.PLAN,
        next_job="GENERATE_PLAN",
        next_artifact_type=ArtifactType.PLAN,
        previous_artifact_type=ArtifactType.INTENT,
        required_headings=(
            "# Specification",
            "## Approved Intent",
            "## Functional Requirements",
            "## Verification Contract",
        ),
    ),
    ArtifactType.PLAN: ArtifactDefinition(
        artifact_type=ArtifactType.PLAN,
        stage=FeatureStage.PLAN,
        display_name="plan.md",
        generate_job="GENERATE_PLAN",
        revise_job="REVISE_PLAN",
        generate_role=AgentRole.PLAN,
        next_stage=FeatureStage.IMPLEMENTATION,
        next_job="PREPARE_IMPLEMENTATION",
        next_artifact_type=None,
        previous_artifact_type=ArtifactType.SPEC,
        required_headings=(
            "# Implementation Plan",
            "## Summary",
            "## Relevant Existing Implementation",
            "## Components / Modules Affected",
            "## Implementation Steps",
            "## File-Level Changes",
            "## Testing Plan",
            "## Verification Mapping",
            "## Risks",
            "## Execution Breakdown",
        ),
    ),
}

JOB_DEFINITIONS: dict[str, tuple[ArtifactDefinition, bool]] = {
    definition.generate_job: (definition, False)
    for definition in ARTIFACT_DEFINITIONS.values()
}
JOB_DEFINITIONS.update(
    {
        definition.revise_job: (definition, True)
        for definition in ARTIFACT_DEFINITIONS.values()
    }
)


class InvalidArtifactError(ValueError):
    pass


def validate_artifact(content: str, definition: ArtifactDefinition) -> None:
    missing = [heading for heading in definition.required_headings if heading not in content]
    if missing:
        raise InvalidArtifactError(
            f"{definition.command_name} is missing required headings: {', '.join(missing)}"
        )


def validate_intent(content: str) -> None:
    validate_artifact(content, ARTIFACT_DEFINITIONS[ArtifactType.INTENT])


def validate_spec(content: str) -> None:
    validate_artifact(content, ARTIFACT_DEFINITIONS[ArtifactType.SPEC])

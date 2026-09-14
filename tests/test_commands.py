from enzo.commands import parse_review_command
from enzo.domain import ArtifactType, CommandAction, ReviewTarget


def test_parse_structured_request_changes() -> None:
    command = parse_review_command(
        "@enzo request-changes intent@3\n"
        "- [Problem] Identify the affected user.\n"
        "- Clarify the success signal."
    )

    assert command is not None
    assert command.action is CommandAction.REQUEST_CHANGES
    assert command.revision == 3
    assert [(item.section, item.comment) for item in command.feedback] == [
        ("Problem", "Identify the affected user."),
        (None, "Clarify the success signal."),
    ]


def test_normal_comment_is_not_a_workflow_command() -> None:
    assert parse_review_command("Looks good to me") is None


def test_parse_spec_approval() -> None:
    command = parse_review_command("@enzo approve spec@4")

    assert command is not None
    assert command.action is CommandAction.APPROVE
    assert command.artifact_type is ArtifactType.SPEC
    assert command.revision == 4


def test_parse_implementation_feedback_resolution() -> None:
    command = parse_review_command("@enzo address-with-agent implementation@2")

    assert command is not None
    assert command.action is CommandAction.ADDRESS_WITH_AGENT
    assert command.target is ReviewTarget.IMPLEMENTATION
    assert command.revision == 2

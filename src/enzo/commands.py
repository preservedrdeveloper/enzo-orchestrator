from __future__ import annotations

import re

from .domain import CommandAction, FeedbackDraft, ReviewCommand, ReviewTarget

COMMAND_RE = re.compile(
    r"^(?:@enzo|/enzo)\s+(approve|request-changes|address-with-agent)\s+"
    r"(intent|spec|plan|implementation)@(\d+)\s*$",
    re.IGNORECASE,
)
FEEDBACK_RE = re.compile(r"^\s*-\s*(?:\[([^\]]+)\]\s*)?(.+?)\s*$")


def parse_review_command(body: str) -> ReviewCommand | None:
    lines = body.strip().splitlines()
    if not lines:
        return None
    match = COMMAND_RE.match(lines[0].strip())
    if not match:
        return None
    action = CommandAction(match.group(1).replace("-", "_").upper())
    target = ReviewTarget(match.group(2).upper())
    feedback: list[FeedbackDraft] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        item = FEEDBACK_RE.match(line)
        if item:
            feedback.append(FeedbackDraft(section=item.group(1), comment=item.group(2)))
    return ReviewCommand(
        action=action,
        target=target,
        revision=int(match.group(3)),
        feedback=tuple(feedback),
    )

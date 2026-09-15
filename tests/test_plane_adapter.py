from __future__ import annotations

import json

import httpx

from enzo.domain import OutboxMessage
from enzo.plane import PlaneProjectBinding, PlaneTaskManagerAdapter


def binding(**overrides) -> PlaneProjectBinding:  # noqa: ANN003
    values = {
        "base_url": "http://plane.test",
        "api_token": "test-token",
        "webhook_secret": "test-secret",
        "workspace_slug": "enzo-lab",
        "project_id": "project-1",
        "artifact_base_url": "http://enzo.test",
        "stage_state_ids": {"INTENT": "state-intent"},
    }
    values.update(overrides)
    return PlaneProjectBinding(**values)


def test_stage_projection_uses_work_item_patch_and_api_key() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "work-item-1"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    result = adapter.deliver(
        OutboxMessage(
            id="outbox-1",
            kind="STAGE_UPDATED",
            external_feature_id="work-item-1",
            idempotency_key="feature:1:stage:intent:v1",
            payload={"stage": "INTENT", "attention": "enzo:running"},
        )
    )

    assert result == "plane:work-item-1:stage"
    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].headers["X-Api-Key"] == "test-token"
    assert requests[0].url.path.endswith("/work-items/work-item-1/")
    assert json.loads(requests[0].content) == {"state": "state-intent"}


def test_review_notice_is_an_idempotent_plane_comment_with_artifact_link() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-1"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    result = adapter.deliver(
        OutboxMessage(
            id="outbox-2",
            kind="REVIEW_REQUIRED",
            external_feature_id="work-item-1",
            idempotency_key="review:1:required",
            payload={
                "artifact": "intent.md",
                "revision_id": "revision-1",
                "revision": 2,
                "addressed_feedback_count": 3,
                "attention": "enzo:needs-review",
            },
        )
    )

    assert result == "comment-1"
    payload = json.loads(requests[0].content)
    assert payload["external_source"] == "enzo"
    assert payload["external_id"].startswith("enzo-")
    assert "http://enzo.test/artifacts/revision-1" in payload["comment_html"]
    assert "@enzo approve intent@2" in payload["comment_html"]
    assert "3 feedback item(s) addressed" in payload["comment_html"]


def test_duplicate_comment_response_is_treated_as_success() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"id": "existing-comment"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    result = adapter.deliver(
        OutboxMessage(
            id="outbox-3",
            kind="COMMAND_REJECTED",
            external_feature_id="work-item-1",
            idempotency_key="command:1:rejected",
            payload={"reason": "stale revision"},
        )
    )

    assert result == "existing-comment"


def test_spec_review_notice_uses_same_plane_review_primitive() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-spec"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    adapter.deliver(
        OutboxMessage(
            id="outbox-spec",
            kind="REVIEW_REQUIRED",
            external_feature_id="work-item-1",
            idempotency_key="review:spec:required",
            payload={
                "artifact": "spec.md",
                "revision_id": "spec-revision-2",
                "revision": 2,
                "addressed_feedback_count": 1,
            },
        )
    )

    payload = json.loads(requests[0].content)
    assert "spec.md revision 2" in payload["comment_html"]
    assert "@enzo approve spec@2" in payload["comment_html"]
    assert "@enzo request-changes spec@2" in payload["comment_html"]


def test_implementation_review_notice_links_external_evidence() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-implementation"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    adapter.deliver(
        OutboxMessage(
            id="outbox-implementation",
            kind="IMPLEMENTATION_REVIEW_REQUIRED",
            external_feature_id="work-item-1",
            idempotency_key="execution:1:review-required",
            payload={
                "execution_id": "execution-1",
                "branch": "ai/work-item-1",
                "base_sha": "base",
                "head_sha": "head",
                "diff_stat": "1 file changed",
            },
        )
    )

    payload = json.loads(requests[0].content)
    assert "IMPLEMENTATION READY FOR REVIEW" in payload["comment_html"]
    assert "http://enzo.test/executions/execution-1" in payload["comment_html"]
    assert "Open implementation review" in payload["comment_html"]
    assert "ai/work-item-1" in payload["comment_html"]
    assert "@enzo approve implementation@1" in payload["comment_html"]
    assert "@enzo request-changes implementation@1" in payload["comment_html"]


def test_implementation_changes_notice_offers_agent_resolution() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-changes"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    adapter.deliver(
        OutboxMessage(
            id="outbox-changes",
            kind="IMPLEMENTATION_CHANGES_REQUESTED",
            external_feature_id="work-item-1",
            idempotency_key="implementation:1:changes",
            payload={"revision": 2, "feedback_count": 3},
        )
    )

    payload = json.loads(requests[0].content)
    assert "3 feedback item(s) recorded" in payload["comment_html"]
    assert "@enzo address-with-agent implementation@2" in payload["comment_html"]


def test_implementation_recovery_notices_explain_retry_and_abandonment() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": f"comment-{len(requests)}"})

    client = httpx.Client(base_url="http://plane.test", transport=httpx.MockTransport(handle))
    adapter = PlaneTaskManagerAdapter(binding(), client=client)
    adapter.deliver(
        OutboxMessage(
            id="outbox-retry",
            kind="IMPLEMENTATION_RETRY_QUEUED",
            external_feature_id="work-item-1",
            idempotency_key="execution:1:retry:2",
            payload={
                "execution_id": "execution-1",
                "operation": "VERIFY_IMPLEMENTATION",
                "attempt": 2,
            },
        )
    )
    adapter.deliver(
        OutboxMessage(
            id="outbox-abandon",
            kind="IMPLEMENTATION_ABANDONED",
            external_feature_id="work-item-1",
            idempotency_key="execution:1:abandoned",
            payload={"execution_id": "execution-1"},
        )
    )

    retry = json.loads(requests[0].content)["comment_html"]
    abandoned = json.loads(requests[1].content)["comment_html"]
    assert "IMPLEMENTATION RETRY QUEUED" in retry
    assert "VERIFY_IMPLEMENTATION" in retry
    assert "attempt 2" in retry
    assert "http://enzo.test/executions/execution-1" in retry
    assert "IMPLEMENTATION ABANDONED" in abandoned
    assert "worktree cleanup" in abandoned
    assert "http://enzo.test/executions/execution-1" in abandoned

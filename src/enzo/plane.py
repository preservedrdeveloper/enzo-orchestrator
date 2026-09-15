from __future__ import annotations

import hashlib
import hmac
import html
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote

import httpx

from .domain import ExternalComment, ExternalFeature, OutboxMessage


class PlaneConfigurationError(ValueError):
    pass


class PlaneWebhookError(ValueError):
    pass


class PlaneWebhookSignatureError(PlaneWebhookError):
    pass


class PlaneAPIError(RuntimeError):
    def __init__(self, method: str, path: str, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.response_body = response.text[:2_000]
        super().__init__(f"Plane API {method} {path} returned {response.status_code}")


def _json_object(value: str | None, *, name: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlaneConfigurationError(f"{name} must be a JSON object") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise PlaneConfigurationError(f"{name} must map strings to strings")
    return parsed


@dataclass(frozen=True)
class PlaneProjectBinding:
    base_url: str
    api_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    workspace_slug: str
    project_id: str
    artifact_base_url: str
    managed_label: str = "enzo:managed"
    stage_state_ids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "base_url": self.base_url,
            "api_token": self.api_token,
            "webhook_secret": self.webhook_secret,
            "workspace_slug": self.workspace_slug,
            "project_id": self.project_id,
            "artifact_base_url": self.artifact_base_url,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise PlaneConfigurationError(f"missing Plane settings: {', '.join(missing)}")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "artifact_base_url", self.artifact_base_url.rstrip("/"))
        object.__setattr__(self, "stage_state_ids", MappingProxyType(dict(self.stage_state_ids)))

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> PlaneProjectBinding:
        return cls(
            base_url=environ.get("ENZO_PLANE_BASE_URL", ""),
            api_token=environ.get("ENZO_PLANE_API_TOKEN", ""),
            webhook_secret=environ.get("ENZO_PLANE_WEBHOOK_SECRET", ""),
            workspace_slug=environ.get("ENZO_PLANE_WORKSPACE_SLUG", ""),
            project_id=environ.get("ENZO_PLANE_PROJECT_ID", ""),
            artifact_base_url=environ.get("ENZO_ARTIFACT_BASE_URL", "http://localhost:8000"),
            managed_label=environ.get("ENZO_PLANE_MANAGED_LABEL", "enzo:managed"),
            stage_state_ids=_json_object(
                environ.get("ENZO_PLANE_STAGE_STATE_IDS"),
                name="ENZO_PLANE_STAGE_STATE_IDS",
            ),
        )


@dataclass(frozen=True)
class ParsedPlaneWebhook:
    delivery_id: str
    event: str
    action: str
    feature: ExternalFeature | None = None
    comment: ExternalComment | None = None
    ignored_reason: str | None = None


class _CommentHTMLParser(HTMLParser):
    _blocks = frozenset({"br", "div", "p"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag in self._blocks:
            if not self.parts or self.parts[-1] != "- ":
                self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" or tag in self._blocks:
            self._newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def plane_comment_text(comment_html: str) -> str:
    parser = _CommentHTMLParser()
    parser.feed(comment_html)
    parser.close()
    return parser.text()


def _string_id(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "")


class PlaneWebhookHandler:
    """Verifies and normalizes Plane webhooks before domain code sees them."""

    def __init__(self, binding: PlaneProjectBinding) -> None:
        self.binding = binding

    def parse(self, *, raw_body: bytes, headers: Mapping[str, str]) -> ParsedPlaneWebhook:
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        delivery_id = normalized_headers.get("x-plane-delivery", "").strip()
        event_header = normalized_headers.get("x-plane-event", "").strip()
        signature = normalized_headers.get("x-plane-signature", "").strip()
        if not delivery_id or not event_header or not signature:
            raise PlaneWebhookError("missing required Plane webhook headers")
        expected = hmac.new(
            self.binding.webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PlaneWebhookSignatureError("invalid Plane webhook signature")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise PlaneWebhookError("invalid Plane webhook JSON") from exc
        if not isinstance(payload, dict):
            raise PlaneWebhookError("Plane webhook payload must be an object")

        event = str(payload.get("event") or "")
        action = str(payload.get("action") or "")
        if event != event_header:
            raise PlaneWebhookError("X-Plane-Event does not match the signed payload")
        if payload.get("workspace_slug") != self.binding.workspace_slug:
            return ParsedPlaneWebhook(delivery_id, event, action, ignored_reason="workspace is not bound")
        data = payload.get("data")
        if not isinstance(data, dict):
            return ParsedPlaneWebhook(delivery_id, event, action, ignored_reason="event has no object data")
        project_id = _string_id(data.get("project_id") or data.get("project"))
        if project_id and project_id != self.binding.project_id:
            return ParsedPlaneWebhook(delivery_id, event, action, ignored_reason="project is not bound")

        # Plane v1.4.x emits past-tense actions (``created``/``updated``),
        # while older webhook examples used imperative forms. Accept both at
        # the adapter boundary and keep the domain event independent of that
        # transport-level difference.
        if event == "issue" and action in {"create", "created", "update", "updated"}:
            if not self._is_managed(data):
                return ParsedPlaneWebhook(
                    delivery_id, event, action, ignored_reason="work item is not Enzo-managed"
                )
            work_item_id = _string_id(data.get("id"))
            title = str(data.get("name") or data.get("title") or "").strip()
            if not work_item_id or not title:
                raise PlaneWebhookError("work-item event is missing id or name")
            return ParsedPlaneWebhook(
                delivery_id,
                event,
                action,
                feature=ExternalFeature(id=work_item_id, title=title),
            )

        if event == "issue_comment" and action in {"create", "created"}:
            body = str(data.get("comment_stripped") or "").strip()
            if not body:
                body = plane_comment_text(str(data.get("comment_html") or ""))
            if not body.lower().startswith(("@enzo ", "/enzo ")):
                return ParsedPlaneWebhook(
                    delivery_id, event, action, ignored_reason="comment is not an Enzo command"
                )
            comment_id = _string_id(data.get("id"))
            work_item_id = _string_id(data.get("issue_id") or data.get("issue"))
            actor_id = _string_id(data.get("actor") or data.get("created_by"))
            if not comment_id or not work_item_id or not actor_id:
                raise PlaneWebhookError("comment event is missing id, issue, or actor")
            return ParsedPlaneWebhook(
                delivery_id,
                event,
                action,
                comment=ExternalComment(
                    id=comment_id,
                    feature_id=work_item_id,
                    actor_id=actor_id,
                    body=body,
                ),
            )

        return ParsedPlaneWebhook(delivery_id, event, action, ignored_reason="unsupported event")

    def _is_managed(self, data: Mapping[str, object]) -> bool:
        if not self.binding.managed_label:
            return True
        labels = data.get("labels")
        if not isinstance(labels, list):
            return False
        for label in labels:
            if isinstance(label, dict) and (
                label.get("name") == self.binding.managed_label
                or label.get("id") == self.binding.managed_label
            ):
                return True
            if label == self.binding.managed_label:
                return True
        return False


class PlaneTaskManagerAdapter:
    """Plane REST boundary. Plane DTOs do not escape this module."""

    def __init__(
        self,
        binding: PlaneProjectBinding,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.binding = binding
        self.client = client or httpx.Client(base_url=binding.base_url, timeout=15)
        self.client.headers.setdefault("X-Api-Key", binding.api_token)
        self.client.headers.setdefault("Accept", "application/json")

    def _work_item_path(self, work_item_id: str, suffix: str = "") -> str:
        workspace = quote(self.binding.workspace_slug, safe="")
        project = quote(self.binding.project_id, safe="")
        work_item = quote(work_item_id, safe="")
        return f"/api/v1/workspaces/{workspace}/projects/{project}/work-items/{work_item}/{suffix}"

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            raise PlaneAPIError(method, path, response)
        return response

    def get_feature(self, work_item_id: str) -> ExternalFeature:
        response = self._request("GET", self._work_item_path(work_item_id))
        data = response.json()
        return ExternalFeature(id=str(data["id"]), title=str(data["name"]))

    def deliver(self, message: OutboxMessage) -> str:
        if message.kind == "STAGE_UPDATED":
            state_id = self.binding.stage_state_ids.get(str(message.payload["stage"]))
            if state_id:
                self._request(
                    "PATCH",
                    self._work_item_path(message.external_feature_id),
                    json={"state": state_id},
                )
            return f"plane:{message.external_feature_id}:stage"

        if message.kind == "ARTIFACT_UPDATING":
            return f"plane:{message.external_feature_id}:updating"

        return self._post_idempotent_comment(
            work_item_id=message.external_feature_id,
            idempotency_key=message.idempotency_key,
            comment_html=self._message_html(message),
        )

    def _post_idempotent_comment(
        self, *, work_item_id: str, idempotency_key: str, comment_html: str
    ) -> str:
        external_id = "enzo-" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        path = self._work_item_path(work_item_id, "comments/")
        response = self.client.post(
            path,
            json={
                "comment_html": comment_html,
                "access": "INTERNAL",
                "external_source": "enzo",
                "external_id": external_id,
            },
        )
        if response.status_code == 409:
            return str(response.json()["id"])
        if response.is_error:
            raise PlaneAPIError("POST", path, response)
        return str(response.json()["id"])

    def _message_html(self, message: OutboxMessage) -> str:
        payload = message.payload
        if message.kind == "REVIEW_REQUIRED":
            revision = int(payload["revision"])
            artifact = str(payload.get("artifact") or "intent.md")
            command_name = artifact.removesuffix(".md")
            artifact_url = (
                f"{self.binding.artifact_base_url}/artifacts/"
                f"{quote(str(payload['revision_id']), safe='')}"
            )
            addressed = int(payload.get("addressed_feedback_count", 0))
            addressed_line = (
                f"<p>{addressed} feedback item(s) addressed in this revision.</p>"
                if addressed
                else ""
            )
            return (
                "<h3>Enzo · NEEDS YOUR REVIEW</h3>"
                f"<p><strong>{html.escape(artifact)} revision {revision}</strong></p>"
                f"{addressed_line}"
                f'<p><a href="{html.escape(artifact_url, quote=True)}">'
                "Open Enzo review surface</a></p>"
                "<p>Plane comment fallback:</p>"
                f"<pre>@enzo approve {html.escape(command_name)}@{revision}</pre>"
                f"<pre>@enzo request-changes {html.escape(command_name)}@{revision}\n"
                "- [Section] Describe the required change.</pre>"
            )
        if message.kind == "CHANGES_REQUESTED":
            revision = int(payload["revision"])
            artifact = str(payload.get("artifact") or "intent.md")
            command_name = artifact.removesuffix(".md")
            return (
                "<h3>Enzo · CHANGES REQUESTED</h3>"
                f"<p>{int(payload['feedback_count'])} feedback item(s) recorded for "
                f"{html.escape(artifact)} revision {revision}.</p>"
                f"<pre>@enzo address-with-agent {html.escape(command_name)}@{revision}</pre>"
            )
        if message.kind == "COMMAND_REJECTED":
            return (
                "<h3>Enzo · Command rejected</h3>"
                f"<p>{html.escape(str(payload['reason']))}</p>"
            )
        if message.kind == "ARTIFACT_FAILED":
            return (
                "<h3>Enzo · Artifact generation failed</h3>"
                f"<pre>{html.escape(str(payload['error'])[:1_000])}</pre>"
            )
        if message.kind == "IMPLEMENTATION_REVIEW_REQUIRED":
            revision = int(payload.get("revision", 1))
            execution_url = (
                f"{self.binding.artifact_base_url}/executions/"
                f"{quote(str(payload['execution_id']), safe='')}"
            )
            return (
                "<h3>Enzo · IMPLEMENTATION READY FOR REVIEW</h3>"
                f"<p><strong>Branch:</strong> {html.escape(str(payload['branch']))}</p>"
                f"<p><strong>Commit:</strong> {html.escape(str(payload['head_sha']))}</p>"
                f'<p><a href="{html.escape(execution_url, quote=True)}">'
                "Open implementation review</a></p>"
                f"<pre>{html.escape(str(payload.get('diff_stat') or 'No diff summary'))}</pre>"
                "<p>Approve or request changes with a new comment:</p>"
                f"<pre>@enzo approve implementation@{revision}</pre>"
                f"<pre>@enzo request-changes implementation@{revision}\n"
                "- [Area] Describe the required code change.</pre>"
            )
        if message.kind == "IMPLEMENTATION_CHANGES_REQUESTED":
            revision = int(payload["revision"])
            return (
                "<h3>Enzo · IMPLEMENTATION CHANGES REQUESTED</h3>"
                f"<p>{int(payload['feedback_count'])} feedback item(s) recorded for "
                f"implementation revision {revision}.</p>"
                f"<pre>@enzo address-with-agent implementation@{revision}</pre>"
            )
        if message.kind == "IMPLEMENTATION_FAILED":
            attempt = int(payload.get("attempt", 1))
            execution_link = ""
            if payload.get("execution_id"):
                execution_url = (
                    f"{self.binding.artifact_base_url}/executions/"
                    f"{quote(str(payload['execution_id']), safe='')}"
                )
                execution_link = (
                    f'<p><a href="{html.escape(execution_url, quote=True)}">'
                    "Open implementation workspace</a></p>"
                )
            return (
                "<h3>Enzo · Implementation execution failed</h3>"
                f"<p><strong>Attempt:</strong> {attempt}</p>"
                f"<pre>{html.escape(str(payload['error'])[:1_000])}</pre>"
                f"{execution_link}"
                "<p>The worktree has been preserved. Open the implementation review "
                "workspace to inspect, retry, or abandon this execution.</p>"
            )
        if message.kind == "IMPLEMENTATION_RETRY_QUEUED":
            execution_url = (
                f"{self.binding.artifact_base_url}/executions/"
                f"{quote(str(payload['execution_id']), safe='')}"
            )
            return (
                "<h3>Enzo · IMPLEMENTATION RETRY QUEUED</h3>"
                f"<p>Retrying {html.escape(str(payload['operation']))} "
                f"(attempt {int(payload['attempt'])}).</p>"
                f'<p><a href="{html.escape(execution_url, quote=True)}">'
                "Open implementation workspace</a></p>"
            )
        if message.kind == "IMPLEMENTATION_ABANDONED":
            execution_url = (
                f"{self.binding.artifact_base_url}/executions/"
                f"{quote(str(payload['execution_id']), safe='')}"
            )
            return (
                "<h3>Enzo · IMPLEMENTATION ABANDONED</h3>"
                "<p>The execution was cancelled and deterministic worktree cleanup "
                "was queued.</p>"
                f'<p><a href="{html.escape(execution_url, quote=True)}">'
                "Open preserved execution history</a></p>"
            )
        raise ValueError(f"unsupported Plane outbox message: {message.kind}")

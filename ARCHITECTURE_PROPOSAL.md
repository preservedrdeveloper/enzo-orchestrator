# Enzo — V0 Architecture Proposal

Status: approved design record. Implemented walking slice: real Plane adapter; reusable
Intent/Spec/Plan review cycles; validated execution breakdown; Project-level
repository binding; deterministic clone/fetch/branch/worktree/commit/push;
FakeAgent coding; revisioned implementation feedback; repeated lint/test/build
evidence; remote-SHA approval fencing; worktree cleanup; and DONE projection.
The opt-in Pi RPC adapter is the first real agent backend. Operator recovery
controls cover deterministic Inspect, Retry, and Abandon behavior.

This document preserves the intended V0 design and therefore includes proposed
objects and steps that are not implemented yet. The README and executable tests
are authoritative for current behavior. In particular, parsed verification
requirements and automatic manual implementation revision detection are still
future work.

Reviewed: 2026-09-14

## Context inspected

The project began as a new Python/FastAPI codebase with no legacy application
constraints.

Plane's current public documentation was checked before designing the adapter. Relevant findings:

- Plane exposes REST resources for work items, project states, comments, links, activities, and attachments.
- Self-hosted instances use the instance's own API base URL and authenticate with `X-API-Key`.
- Current integrations must use `/work-items/`; Plane ended support for the former `/issues/` endpoints on 2026-03-31.
- Work-item state can be changed with `PATCH .../work-items/{work_item_id}/` and a Plane state UUID.
- Webhooks include unique `X-Plane-Delivery`, event name, and HMAC signature headers. The documented webhook event names still use `issue` and `issue_comment`, even though REST resources now use `work-items`.
- Comments support `external_source` and `external_id`, which are useful for reconciliation of orchestrator-authored comments.
- Work-item links can reference the orchestrator's artifact viewer.
- The documented API does not expose a way to inject custom Approve/Request Changes buttons into Plane's native work-item UI.

Sources:

- https://developers.plane.so/api-reference/introduction
- https://developers.plane.so/api-reference/issue/overview
- https://developers.plane.so/api-reference/issue/update-issue-detail
- https://developers.plane.so/api-reference/issue-comment/overview
- https://developers.plane.so/api-reference/issue-comment/add-issue-comment
- https://developers.plane.so/api-reference/link/add-link
- https://developers.plane.so/api-reference/state/list-states
- https://developers.plane.so/dev-tools/intro-webhooks

## 1. Architecture proposal

V0 is one deployable application plus one worker process, one SQLite database, and filesystem storage. Plane remains the human-facing control plane. The orchestrator owns workflow truth and performs all deterministic lifecycle operations.

```text
Human in Plane
  │ work item/comment actions
  ▼
Plane webhooks ──► FastAPI ingress ──► inbox_events
                                         │
                                         ▼
                                  deterministic reducer
                                  State + Event → Actions
                                         │
                      ┌──────────────────┼──────────────────┐
                      ▼                  ▼                  ▼
                 artifact job      execution job       Plane outbox
                      │                  │                  │
                      ▼                  ▼                  ▼
                 AgentRunner       Git/Worktree +      TaskManagerAdapter
                                  CommandRunner
                      │                  │
                      └──────────┬───────┘
                                 ▼
                       SQLite + filesystem artifacts
```

The reducer is the center of the system. It accepts a validated event, loads current state, checks transition preconditions, writes the state change and durable jobs in one SQLite transaction, and returns. Workers execute jobs later and emit new structured events back through the same transition path.

There is no generic agent-to-agent protocol. Agents are replaceable workers behind `AgentRunner`; they cannot advance a stage, approve a review, create or remove a worktree, commit, push, or declare verification passed.

### Deployment shape

- `api`: FastAPI webhook receiver plus the focused Enzo artifact review surface.
- `worker`: a single durable SQLite-backed job loop with leases. Concurrency defaults to one; project config may cap it at two.
- `db`: SQLite in WAL mode.
- `storage`: local filesystem for revision directories, logs, diffs, and evidence.
- `repo cache`: one managed clone/mirror per configured project.
- `worktrees`: one active worktree per feature implementation lifecycle.

No Redis, Celery, Kafka, Kubernetes, or separate workflow service is needed to test the hypothesis.

## 2. Exact V0 scope

### Included vertical slice

1. Register a Plane project and bind it to one Git monorepo plus deterministic commands.
2. Ingest a Plane work item marked with the configured `enzo:managed` label as a Feature in `IDEA`.
3. Generate `intent.md`; notify a human in Plane; accept explicit approval or requested changes.
4. Store feedback as separate `FeedbackItem` rows; let a human choose manual edit or agent resolution.
5. Repeat the same review primitive for `spec.md` and `plan.md`.
6. Store the Verification Contract in each immutable `spec.md`; structured
   requirement extraction is a future extension.
7. Store a machine-readable `execution-plan.yaml` beside each plan revision; validate dependencies, but execute sequentially.
8. On plan approval, prepare the repository, base SHA, branch, and isolated worktree deterministically.
9. Invoke one configured coding backend through `AgentRunner` inside the
   worktree. Pi RPC is the first real adapter and FakeAgent is the deterministic
   default.
10. Run configured lint/test/build commands independently of the coding agent.
11. If checks pass, commit, push with generic Git, record SHA/diff/evidence, and request implementation review.
12. On implementation feedback, reuse the same worktree, create a new implementation revision, rerun all required evidence, and request review again.
13. On final approval, verify the recorded commit and remote branch, close the execution, remove the worktree, prune, and mark the feature `DONE`.
14. Expose read-only Inspect plus explicit Retry and Abandon controls for failed
    executions. Retry requeues the exact failed durable job in the same
    Execution/worktree; Abandon queues deterministic managed-worktree cleanup.

### Practical manual editing

Artifact links posted to Plane open a narrow artifact page owned by the orchestrator. The page can display revision history and, while the current revision is `CHANGES_REQUESTED`, accept a complete Markdown replacement that creates a new revision. It is not a project-management UI. All stage visibility, notifications, feedback, and approval decisions remain in Plane.

## 3. Smallest coherent domain model

| Object | Purpose | Important fields |
|---|---|---|
| Project | Plane project to monorepo binding | Plane IDs, repo URL, default branch, command config, Git/cleanup policy |
| Feature | One managed Plane work item | Plane work-item ID, identifier, title, stage, version |
| Artifact | Stable logical artifact identity | feature, type, display name, current revision |
| ArtifactRevision | Immutable content revision plus review status | revision number, status, content path/hash, parent, author, timestamps |
| ArtifactReference | Version-pinned relationship to UX/supporting artifacts | from revision, to revision or external URI, relation, metadata |
| Review | One human decision gate | target artifact/implementation revision, status, reviewer, source event |
| FeedbackItem | One explicit requested change | target revision, section, comment, status, resolution type/result |
| ExecutionPlan | Parsed machine-readable plan bound to an approved plan revision | version, source revision |
| ExecutionTask | Sequential V0 task with future DAG metadata | key, description, dependencies, order |
| Execution | Implementation lifecycle and deterministic Git workspace | status, base/head SHA, branch, worktree, current attempt |
| ExecutionAttempt | One coder/feedback-resolver try in the same worktree | attempt, role, status, agent run, result/error |
| ImplementationRevision | Reviewable code snapshot | execution attempt, head SHA, diff path/hash, status |
| VerificationRequirement | Parsed requirement from an approved spec | stable key, evidence type, required assertion/config |
| VerificationEvidence | External result for one implementation revision | requirement key, status, command, exit code, log/artifact path/hash |
| AgentRun | Auditable invocation of the configured backend | role, input manifest/hash, backend, status, output/logs |
| InboxEvent / Job / OutboxMessage / DomainEvent | Reliability and audit primitives | idempotency key, payload, status, attempts, timestamps |

An implementation revision uses the same review and feedback semantics as Markdown but is not forced into `ArtifactRevision`. Its content identity is a Git SHA plus captured diff and evidence.

## 4. State machines

### Feature stage

```text
IDEA
  └─ intake accepted ─► INTENT
INTENT
  └─ current intent revision APPROVED ─► SPEC
SPEC
  └─ current spec revision APPROVED ─► PLAN
PLAN
  └─ current plan revision APPROVED ─► IMPLEMENTATION
IMPLEMENTATION
  └─ code candidate ready ─► VERIFICATION
VERIFICATION
  ├─ evidence failed / implementation feedback ─► IMPLEMENTATION
  └─ implementation revision APPROVED ─► DONE
```

Only the orchestrator writes Plane's feature-stage state. Agents never transition it. Moving a Plane card manually does not count as approval; V0 reconciles it back to the authoritative stage and posts guidance.

### Artifact revision status

```text
GENERATING ─► REVIEW ─► APPROVED
     │           │
     │           └─► CHANGES_REQUESTED ─► UPDATING ─► REVIEW (new revision)
     │                                      │
     └──────────────────────────────────────┴─► FAILED
```

`UPDATING` belongs to the newly reserved revision. The reviewed prior revision remains `CHANGES_REQUESTED`. Content is never overwritten. A manual edit and an agent edit both create a child revision.

### Execution status

```text
PENDING ─► RUNNING ─► SUCCEEDED ─► CLOSED
               │           │
               ├─► FAILED  └─► RUNNING (feedback attempt, same worktree)
               └─► CANCELLED

FAILED ─► PENDING/RUNNING (Retry) | CANCELLED (Abandon)
CANCELLED ─► worktree cleanup (status remains CANCELLED)
```

`SUCCEEDED` means a pushed reviewable implementation exists; it does not mean the Feature is done. `CLOSED` follows human approval or explicit abandonment and cleanup policy.

## 5. Artifact lifecycle and filesystem layout

Each revision is written into a new directory and finalized atomically. A manifest records hashes and provenance.

```text
var/artifacts/features/<feature-id>/
  intent/r0001/
    intent.md
    manifest.json
  spec/r0001/
    spec.md
    verification-contract.yaml
    manifest.json
  plan/r0001/
    plan.md
    execution-plan.yaml
    manifest.json
  ux/<ux-artifact-id>/r0001/
    mock.png
    manifest.json
  implementation/r0001/
    diff.patch
    summary.md
    manifest.json
  verification/r0001/
    verification-report.md
    evidence/
      lint.log
      test.log
      build.log
    manifest.json
```

Rules:

- Paths are generated by the application, never supplied directly by Plane or an agent.
- Write to a sibling temporary directory, fsync important files, then rename atomically.
- Store SHA-256 for every stored file in `manifest.json` and SQLite.
- Approval changes metadata/status only; it never mutates revision content.
- References are pinned to a revision ID and may include viewport metadata.
- The UI may display `spec.md`, but URLs include the revision ID.
- Agent inputs use explicit manifests listing only the current artifact, unresolved feedback, approved upstream revisions, referenced UX artifacts, and repository workspace.

Example verification contract:

```yaml
version: 1
requirements:
  - id: carrier-query-parameter
    type: integration_test
    required: true
    assertion: selecting MAERSK adds the expected search query parameter
  - id: carrier-filter-mobile
    type: screenshot
    required: true
    viewport: {width: 390, height: 844}
    reference_artifact_revision: ux-rev-id
  - id: visual-approval
    type: human_visual_approval
    required: true
```

V0 captures screenshot evidence when a configured repository command produces it, but performs no sophisticated pixel diff.

Example execution plan:

```yaml
version: 1
tasks:
  - id: domain-model
    title: Extend domain model
    depends_on: []
  - id: backend-filtering
    title: Implement backend filtering
    depends_on: [domain-model]
  - id: frontend-filter
    title: Implement frontend filter
    depends_on: [domain-model]
verification_mapping:
  carrier-query-parameter:
    tasks: [backend-filtering]
    evidence: [integration_test]
```

The parser rejects missing IDs, dependency cycles, unknown verification keys, and unsafe paths. V0 then topologically sorts and runs one task at a time.

## 6. Reusable review and feedback lifecycle

The exact same application service handles Intent, Spec, and Plan:

1. Reserve revision with `GENERATING` or `UPDATING`.
2. Invoke `AgentRunner` or accept a manual full-document edit.
3. Validate required artifact shape and companion YAML.
4. Finalize files and set the new revision to `REVIEW`.
5. Create a `PENDING` Review.
6. Set Plane attention to `enzo:needs-review`, create/update the artifact link, and post a review summary.
7. Wait. No timer or worker can cross this gate.
8. On APPROVE, verify the command targets the current revision and reviewer is allowed, close the Review, approve the revision, resolve the gate, and enqueue the next stage action atomically.
9. On REQUEST_CHANGES, create one `FeedbackItem` per submitted item, set the revision to `CHANGES_REQUESTED`, and wait for manual edit or `address-with-agent`.
10. Agent resolution receives only unresolved feedback. A new revision is created; each addressed item records `resolution_type=AGENT` and `resolved_in_revision_id` only when the new revision reaches review.
11. Manual replacement follows the same rule with `resolution_type=MANUAL`.

Implementation reviews call the same Review/Feedback service with an `ImplementationRevision` target. The resolver role is `CODER`/`FEEDBACK_RESOLVER`, and a new reviewable revision requires a new commit and fresh verification evidence.

### Plane command grammar for V0

Commands are accepted only from configured reviewer Plane user IDs and only for the current revision:

```text
@enzo approve spec@3

@enzo request-changes spec@3
- [UX / Carrier Selector] Use the existing mobile filter drawer.
- [Security] Clarify tenant authorization for the endpoint.

@enzo address-with-agent spec@3
@enzo retry execution@<id>
@enzo inspect execution@<id>
@enzo abandon execution@<id>
```

Each bullet after `request-changes` becomes its own FeedbackItem. A comment without a valid command remains normal discussion and causes no state transition. Stale-revision decisions are rejected with a helpful Plane comment.

## 7. Deterministic worktree lifecycle

The application, never the LLM, runs this sequence:

1. Lock the project/feature execution.
2. Fetch the managed repo cache.
3. Resolve and persist `base_sha` from the configured default branch.
4. Derive a sanitized branch such as `ai/123-carrier-filter`; reject collisions not owned by this feature.
5. Create the branch and an isolated worktree at a path derived from Execution ID.
6. Run configured bootstrap command if its policy says it is needed.
7. Invoke the coding agent with `cwd` fixed to the worktree and workspace-write permission only.
8. Capture changed files and reject writes outside the worktree.
9. Run configured deterministic verification commands with timeouts and captured stdout/stderr.
10. On pass, create a deterministic application-owned commit with an `SDLC-Execution` trailer.
11. Push using generic Git, then verify the remote ref resolves to the recorded SHA.
12. Capture diff, check results, evidence hashes, and implementation revision.
13. Keep the worktree through implementation review and all feedback attempts.
14. On approval, re-check local commit and remote ref, set Execution `CLOSED`, remove the exact registered worktree, and run `git worktree prune`.

Git credentials are available only to the deterministic fetch/push subprocess. They are not placed in the agent environment or passed in arguments. Repository commands are admin-configured argument arrays, not text from Plane or the agent, and run without a shell by default.

Failure policy:

- Coding/check/push failure: preserve worktree; expose Retry, Inspect, Abandon.
- Retry: same Execution and worktree; increment the exact durable job attempt.
- Inspect: publish sanitized logs, evidence, and the recorded diff without mutation.
- Abandon: explicit human action; mark `CANCELLED` and force-remove only the
  registered path beneath the managed worktree root. Preserve evidence/history.
- Service crash: reconcile registered worktree, branch, SHA, job lease, and remote state before doing anything again.

## 8. Plane integration approach

`PlaneTaskManagerAdapter` implements a provider-neutral interface:

```python
class TaskManagerAdapter(Protocol):
    def get_feature(self, ref: ExternalFeatureRef) -> ExternalFeature: ...
    def update_stage(self, ref: ExternalFeatureRef, stage: FeatureStage) -> None: ...
    def get_feedback_source(self, ref: ExternalFeatureRef, comment_id: str) -> ExternalComment: ...
    def publish_artifact(self, ref: ExternalFeatureRef, artifact: PublishedArtifact) -> None: ...
    def notify_review_required(self, ref: ExternalFeatureRef, notice: ReviewNotice) -> None: ...
```

Plane API DTOs end inside the adapter and are translated into these internal types.

### Plane responsibilities and mapping

| Need | Plane mechanism |
|---|---|
| Identify managed feature | project binding + `enzo:managed` label + work-item UUID |
| Show feature stage | seven project states: IDEA, INTENT, SPEC, PLAN, IMPLEMENTATION, VERIFICATION, DONE |
| Show attention | exactly one operational label: `enzo:running`, `enzo:needs-review`, `enzo:changes-requested`, or `enzo:failed` |
| Notify review | orchestrator-authored work-item comment |
| Link artifact/revision history | work-item link to authenticated artifact viewer |
| Capture approval/change request | explicit `@enzo ...` comment command received by comment webhook |
| Capture feedback | structured bullets in the change-request comment; one DB row per bullet |
| UX input | linked URI or Plane attachment imported and hashed as a UX artifact revision |

### Webhook handling

- Receive raw request bytes, delivery/event/signature headers, and timestamp.
- Verify HMAC against raw bytes before JSON parsing. Plane's example serializes JSON; raw-byte signing must be confirmed with the selected self-hosted Plane version during the integration spike.
- Insert `X-Plane-Delivery` into `inbox_events` under a unique constraint, return 200 quickly, and process asynchronously.
- Normalize documented `issue` and `issue_comment` event names to internal work-item events.
- Re-fetch the work item/comment via REST before acting; do not trust a partial or version-specific webhook payload.
- Ignore orchestrator comments by `external_source` and known `external_id`.
- On uncertain POST/PATCH results, reconcile by fetching current state/comments/links before retrying.

The adapter will be tested against the exact target self-hosted Plane edition/version because webhook naming and payload details can lag the REST naming in public docs.

## 9. Agent boundary

```python
class AgentRunner(Protocol):
    @property
    def spec(self) -> AgentRunnerSpec: ...

    def run(self, request: AgentRequest) -> AgentResult: ...

@dataclass(frozen=True)
class AgentRequest:
    role: AgentRole
    workload: AgentWorkload
    project_id: str
    feature_id: str
    artifact_revision_id: str
    workspace_path: str | None
    current_content: str | None
    unresolved_feedback: tuple[FeedbackDraft, ...]
    metadata: dict[str, Any]
```

Roles: `INTENT`, `SPEC`, `PLAN`, `CODER`, `FEEDBACK_RESOLVER`; `VERIFIER` is defined but optional in V0.

`AgentRunnerRouter` selects a registered runner deterministically by workload,
then role, then default. Workload is separate from role because artifact and
implementation revisions both use `FEEDBACK_RESOLVER` but have different
capability requirements. A runner advertises `GENERATE_TEXT` and/or
`EDIT_WORKSPACE`; incompatible routes fail before invocation. Concrete runner,
adapter kind, provider, and model provenance are recorded with each run.
Adapters normalize operational failures into `AgentRunnerError` with a stable
failure kind, concrete runner name, and retryable flag; artifact/schema
validation remains an orchestration concern after the runner returns.

The first real backend is `PiAgentRunner`, using an ephemeral RPC subprocess
with strict JSONL, normalized failures, timeout/process-group cleanup, and
durable transcripts. Codex CLI and Claude Code headless mode can later be
implemented as peer adapters without changing orchestration semantics.

Pi receives no tools for artifact generation and a shell-free file tool set for
implementation. Extensions, skills, prompt templates, context files, and
project-local trust are disabled. Enzo passes an environment allowlist rather
than leaking Plane/Git credentials to the child process. Because Pi has no
built-in sandbox, container/OS isolation remains required before unattended
execution against untrusted repositories.

`AgentRoutingPolicy` is also a port. The V0 environment-backed policy is
global, but `project_id` is explicit on every request so project-scoped runner
selection can replace it later without coupling runners to SQLite or Plane.

Test backend: `FakeAgentRunner` maps request fixtures to deterministic outputs and failures.

The runner can write a candidate artifact or modify code, but its result is only an input to application validation. Agent text such as "tests pass" creates no evidence and changes no state.

## 10. Persistence schema

Proposed SQLite tables and essential constraints:

```text
projects
  id PK, slug UNIQUE, plane_workspace_slug, plane_project_id UNIQUE,
  plane_base_url, repository_config_json, commands_json, policies_json

features
  id PK, project_id FK, plane_work_item_id, plane_identifier, title,
  stage CHECK(...), version, created_at, updated_at,
  UNIQUE(project_id, plane_work_item_id)

artifacts
  id PK, feature_id FK, type, display_name, current_revision_id,
  UNIQUE(feature_id, type)

artifact_revisions
  id PK, artifact_id FK, revision_no, parent_revision_id FK NULL,
  status CHECK(...), content_path, manifest_path, content_sha256,
  created_by_kind, created_by_id, agent_run_id FK NULL, created_at,
  UNIQUE(artifact_id, revision_no)

artifact_references
  id PK, from_revision_id FK, to_revision_id FK NULL,
  external_uri NULL, relation, metadata_json,
  CHECK(exactly one of to_revision_id/external_uri)

reviews
  id PK, artifact_revision_id FK NULL, implementation_revision_id FK NULL,
  status, reviewer_external_id, source_event_id, opened_at, decided_at,
  CHECK(exactly one target)

feedback_items
  id PK, review_id FK, artifact_revision_id FK NULL,
  implementation_revision_id FK NULL, source_comment_id, source_ordinal,
  section, comment, status, resolution_type, resolved_by,
  resolved_in_artifact_revision_id FK NULL,
  resolved_in_implementation_revision_id FK NULL, created_at, resolved_at,
  UNIQUE(source_comment_id, source_ordinal)

verification_requirements
  id PK, spec_revision_id FK, requirement_key, evidence_type,
  required, definition_json, UNIQUE(spec_revision_id, requirement_key)

execution_plans
  id PK, plan_revision_id FK UNIQUE, schema_version, source_path, source_sha256

execution_tasks
  id PK, execution_plan_id FK, task_key, ordinal, title,
  depends_on_json, definition_json, UNIQUE(execution_plan_id, task_key)

executions
  id PK, feature_id FK, project_id FK, execution_plan_id FK,
  status, base_sha, branch, worktree_path, head_sha,
  current_attempt, started_at, finished_at, result_json, error,
  UNIQUE(feature_id, execution_plan_id)

execution_attempts
  id PK, execution_id FK, attempt_no, role, status,
  agent_run_id FK NULL, started_at, finished_at, result_json, error,
  UNIQUE(execution_id, attempt_no)

implementation_revisions
  id PK, execution_id FK, attempt_id FK, revision_no, parent_revision_id FK NULL,
  head_sha, diff_path, diff_sha256, status, created_at,
  UNIQUE(execution_id, revision_no)

verification_evidence
  id PK, implementation_revision_id FK, requirement_id FK NULL,
  evidence_key, evidence_type, required, status, command_json,
  exit_code, artifact_path, artifact_sha256, metadata_json, created_at,
  UNIQUE(implementation_revision_id, evidence_key)

agent_runs
  id PK, role, backend, status, request_manifest_path,
  request_sha256, output_path, output_sha256, log_path,
  started_at, finished_at, error

inbox_events
  id PK, source, delivery_id UNIQUE, event_type, payload_json,
  payload_sha256, status, received_at, processed_at, error

jobs
  id PK, kind, idempotency_key UNIQUE, payload_json, status,
  attempts, available_at, lease_owner, lease_expires_at, last_error

outbox_messages
  id PK, destination, idempotency_key UNIQUE, payload_json,
  status, attempts, external_id, last_error

domain_events
  id PK, feature_id FK NULL, event_type, actor_type, actor_id,
  subject_type, subject_id, payload_json, occurred_at
```

Artifact and implementation statuses are stored on the current revision; Feature stage is independent. `domain_events` is enough to calculate Human Touches Per Completed Feature later without building analytics now.

## 11. Failure and idempotency strategy

### Idempotency keys

- Webhook: Plane `X-Plane-Delivery`.
- Review command: Plane comment ID plus command ordinal.
- Generation: `generate:<feature>:<artifact-type>:<parent-revision-or-root>`.
- Feedback resolution: `resolve:<target-revision>:<sorted-open-feedback-ids-hash>`.
- Execution: `execute:<feature>:<approved-plan-revision>`.
- Verification: `verify:<implementation-revision>`.
- Plane notification: `plane:<event-or-review-id>:<notification-kind>`.

### Guarantees

- A state transition and its jobs/outbox writes share one SQLite transaction.
- Feature rows use optimistic `version` checks; workers also take narrow DB leases.
- Duplicate delivery or command returns success without redoing work.
- A job can run at least once. Every side effect is reconciled before retry.
- Atomic filesystem finalize prevents partially visible artifact revisions.
- Git preparation checks registered branch/worktree ownership before create.
- Commit retry searches for the application trailer and validates the tree SHA.
- Push retry compares the remote ref to recorded SHA.
- Plane comment retry first searches for matching `external_source`/`external_id`; API uniqueness is not assumed.

### Failure surfaces

- Invalid agent output: revision `FAILED`, stage unchanged, explicit retry notice.
- Invalid companion YAML: same as invalid output; never reaches review.
- Deterministic check failure: evidence records FAIL, execution remains inspectable, worktree retained.
- Push uncertain: reconcile remote ref before retry or failure.
- Plane temporarily unavailable: local state remains authoritative; outbox retries with backoff.
- Crash with expired job lease: next worker reconciles external state and continues.
- Cleanup failure after approval: Feature may be DONE while Execution is `CLOSED` with `cleanup_pending`; retry cleanup without reopening review.

## 12. Current repository structure

```text
AI Native SDLC/
  ARCHITECTURE_PROPOSAL.md
  README.md
  pyproject.toml
  .env.example
  src/enzo/
    artifacts/
      definitions.py       # reusable artifact state metadata and validation
      execution_plan.py    # machine-readable Plan schema
      store.py             # immutable filesystem revisions
    execution/
      models.py            # Project config and execution values
      git.py               # deterministic generic Git/worktree operations
      commands.py          # shell-free deterministic command runner
      coordinator.py       # durable prepare → code → verify pipeline
    agents/
      errors.py            # normalized adapter failure taxonomy
      factory.py           # composition root for installed runner adapters
      pi.py                # Pi RPC process/protocol adapter
      prompts.py           # provider-neutral prompt compiler
      routing.py           # runner registry, selection policy, capability checks
    domain.py              # small shared vocabulary and port values
    orchestrator.py        # workflow reducer and review state machine
    database.py            # SQLite schema and forward-only migrations
    plane.py               # TaskManagerAdapter implementation
    ports.py               # AgentRunner and TaskManagerAdapter boundaries
    fakes.py               # deterministic V0/demo backends
    web.py
    worker.py
  tests/
  var/                 # ignored runtime data
    artifacts/
    repositories/
    worktrees/
    executions/
```

The package stays intentionally compact. Subdirectories mark the two areas
with independent lifecycle rules—immutable artifacts and mutable execution
workspaces—rather than introducing framework-style layering.

## 13. Implementation sequence after approval

1. Bootstrap package, settings, SQLite migrations, enums, and transition-table tests.
2. Implement artifact filesystem store, immutable revisions, manifests, and review/feedback service using `FakeAgentRunner`.
3. Implement durable inbox/jobs/outbox and end-to-end Intent → feedback → revise → approve tests.
4. Extend the same primitive to Spec and Plan; validate verification contract and execution plan schemas.
5. Implement generic `TaskManagerAdapter`, fake adapter, then a Plane compatibility spike against the target self-hosted version: auth, webhook signature, event names, work item fetch/update, comment ingestion, link publishing.
6. Implement Plane adapter and replay-safe webhook tests using recorded sanitized payload fixtures.
7. **Complete:** deterministic Git/worktree and command runners with temporary local bare remotes in integration tests.
8. **Complete:** Execution, implementation revisions, feedback, human decisions,
   evidence capture, remote fencing, and cleanup with `FakeAgentRunner`.
9. **Complete:** multi-runner registry, workload/role routing, capability checks,
   and concrete-runner provenance.
10. **Complete:** Pi RPC adapter with timeout/process cleanup, JSONL event
    capture, output validation, environment allowlisting, and safe tool policy.
11. **Complete:** implementation feedback reuses the active worktree and reruns verification.
12. **Complete:** final approval, remote-SHA check, and cleanup. Explicit
    retry/inspect/abandon commands remain to be implemented.
13. Run one real vertical-slice feature against a disposable test monorepo and the selected Plane instance; record lessons before adding scope.

The first demo should optimize for observability and correctness of gates, not UI polish or concurrency.

## 14. Explicitly not being built in V0

- A generic multi-agent framework or agent collaboration protocol.
- A replacement project-management UI.
- Automatic merge, deployment, release, or production changes.
- GitHub-, GitLab-, Gitee-, or other provider-specific APIs.
- Pull-request automation.
- Additional real runner adapters beyond Pi.
- Sophisticated independent verifier agent; only its interface is reserved.
- Distributed DAG scheduling or parallel task execution.
- Redis, Celery, Kafka, Kubernetes, or distributed locks.
- S3/object storage.
- Repository RAG, embeddings, vector search, or a code knowledge graph.
- Figma integration.
- Sophisticated screenshot or perceptual diffing.
- Fine-grained permission system beyond configured reviewer allowlists and Plane/API authentication.
- Generalized workflow DSL or user-defined stages.
- Automatic garbage collection.
- Analytics dashboards; only structured events are retained.
- Automatic rollback of repository or Plane changes.

## Assumptions and items needing review

1. **Plane interaction:** V0 uses explicit comment commands because current public APIs do not provide native custom review buttons. A tiny artifact viewer/editor is linked from Plane, but approval stays in Plane.
2. **Plane version:** The target self-hosted edition/version and API availability are not yet known. The adapter spike must validate Personal Access Tokens, webhook configuration, signature bytes, event names, comment actor IDs, and attachment download behavior on that exact instance.
3. **Stage authority:** The orchestrator is authoritative. Manual Plane card movement is not approval and will be reconciled. If stage movement should itself mean approval, that is a different and less explicit interaction contract.
4. **Artifact hosting:** Plane links need an HTTP(S)-reachable orchestrator base URL. Local `file://` paths are not sufficient for reviewers.
5. **Repository access:** The runtime host can fetch and push the configured generic Git remote with non-interactive credentials. Auto-push defaults on; auto-merge remains off.
6. **Agent backend:** Pi RPC is the first real adapter. Authentication and
   provider/model selection remain operational configuration rather than domain
   logic; the first authenticated model smoke test is still pending.
7. **Verification:** lint/test/build are required when configured. E2E and screenshot requirements are only satisfiable when the bound repository supplies deterministic commands that produce those outputs.
8. **Manual edits:** Full-document edits create a new revision. Line-level collaborative editing is out of scope.
9. **One active implementation:** V0 permits only one active Execution per Feature. Execution tasks retain dependency metadata but run sequentially in the same worktree.
10. **Security:** Plane/API credentials and Git credentials are stored outside SQLite artifact content and are never included in agent context.

## Recommended approval decision

Approve this architecture if the experiment to validate is specifically:

> Can a deterministic reducer move one feature through versioned Intent, Spec, Plan, code, and evidence while agents only produce candidates and humans explicitly authorize every semantic gate?

The most important V0 learning signal is not how many agents it can run. It is whether revision history, explicit feedback, deterministic transitions, and evidence-backed implementation review make the autonomous parts trustworthy enough that humans mostly review instead of operate.

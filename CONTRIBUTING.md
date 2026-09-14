# Contributing to Enzo

Enzo is an experiment in state-driven, artifact-first software delivery. Keep
changes focused on that experiment rather than turning the project into a
generic agent framework or project-management system.

## Development setup

Enzo requires Python 3.12 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install '.[dev]'
.venv/bin/pytest
```

Before opening a pull request, run:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src
node --check src/enzo/review_ui/app.js
```

## Architecture invariants

- Plane is a human-facing projection; Enzo owns orchestration truth.
- Feature stage, artifact status, review status, and execution status stay
  separate.
- Intent, Spec, and Plan share one parameterized review state machine.
- Human gates are explicit and never crossed by an agent.
- Approved or reviewed artifacts are immutable; changes create revisions.
- Agents do not own workflow transitions, Git lifecycle, or verification
  outcomes.
- Git and configured verification commands run without a shell.
- External deliveries and UI mutations must be idempotent.
- New public adapters belong behind the existing ports instead of leaking
  provider DTOs into domain code.

## Pull requests

Include tests for state transitions, stale events, and idempotency whenever a
change affects orchestration. Preserve unrelated local changes and never add
credentials, `.env.local`, runtime artifacts, repository caches, or worktrees.

For security-sensitive changes, follow [SECURITY.md](SECURITY.md) rather than
opening a public issue with exploit details.

# Plane integration smoke test

- Date: 2026-09-14
- Plane: Community Edition v1.4.2
- Enzo agent backend: `FakeAgentRunner`

## Fixture

- Workspace: `AI Native SDLC Lab` (`ai-native-sdlc-lab`)
- Project: `SDLC Integration Test` (`SDLC`)
- Work item: `SDLC-1` — `Enzo Live Test · Carrier Filter`
- Work-item ID: `fb1bdf95-53e1-4546-8f1a-a246b365060a`
- Managed label: `enzo:managed`

## Observed flow

```text
Plane work item created
→ signed issue webhook accepted
→ intent.md revision 1 generated
→ Enzo review notice published to Plane
→ two structured feedback items submitted in a Plane comment
→ artifact status CHANGES_REQUESTED; unresolved feedback = 2
→ @enzo address-with-agent intent@1
→ intent.md revision 2 generated with revision 1 as parent
→ both feedback items resolved by the agent revision
→ @enzo approve intent@2
→ revision 2 APPROVED
→ Enzo feature stage SPEC
→ Plane state Enzo · Spec
→ GENERATE_SPEC left PENDING at the intentional V0 boundary
```

## Evidence

- Artifact revisions: 2 immutable filesystem revisions with distinct SHA-256 hashes.
- Feedback: 2 first-class records, both resolved in revision 2.
- Jobs: `GENERATE_INTENT` and `REVISE_INTENT` succeeded once; `GENERATE_SPEC` is pending.
- Outbox: two stage projections, two review notices, one changes-requested notice,
  and one updating event were delivered exactly once.
- Plane webhook actions in v1.4.2 are past tense (`created`, `updated`). The adapter
  accepts those values as well as the older imperative forms.

This record captures the first Intent-only walking slice. Migration 2 preserves
its immutable Intent revisions and cancels its pre-versioned `GENERATE_SPEC`
placeholder. The expanded Intent + Spec smoke test is recorded separately.

# Plane Intent + Spec smoke test

- Date: 2026-09-14
- Plane: Community Edition v1.4.2
- Enzo agent backend: `FakeAgentRunner`

## Fixture

- Workspace: `AI Native SDLC Lab` (`ai-native-sdlc-lab`)
- Project: `SDLC Integration Test` (`SDLC`)
- Work item: `SDLC-2` — `Enzo Live Test · Reusable Intent and Spec Review`
- Work-item ID: `7c6a62a6-4a2e-428d-8378-1d8888c62fce`
- Managed label: `enzo:managed`

## Observed flow

```text
Plane work item created
→ intent.md revision 1 generated
→ human approval through @enzo approve intent@1
→ Intent revision 1 APPROVED
→ feature and Plane state moved to SPEC
→ spec.md revision 1 generated with approved intent.md as explicit context
→ two structured Spec FeedbackItems created
→ spec.md revision 1 CHANGES_REQUESTED
→ @enzo address-with-agent spec@1
→ spec.md revision 2 generated with revision 1 as its parent
→ both Spec FeedbackItems resolved by the new agent revision
→ @enzo approve spec@2
→ Spec revision 2 APPROVED
→ feature and Plane state moved to PLAN
→ GENERATE_PLAN left PENDING at the intentional slice boundary
```

## Evidence

- `intent.md` revision 1: `APPROVED`, SHA-256 prefix `077af99434b3`.
- `spec.md` revision 1: `CHANGES_REQUESTED`, SHA-256 prefix `50cf02b0d176`.
- `spec.md` revision 2: `APPROVED`, SHA-256 prefix `2f4f95a9f3d6`.
- Spec feedback sections: `User Experience` and `Verification Contract`.
- Both feedback records: `RESOLVED` with resolution type `AGENT`, pinned to Spec revision 2.
- Jobs: `GENERATE_INTENT`, `GENERATE_SPEC`, and `REVISE_SPEC` each succeeded once;
  `GENERATE_PLAN` is pending.
- Outbox: three stage projections, three review notices, one changes-requested
  notice, and one updating event were all delivered.
- Plane and Enzo independently reported the final stage as `PLAN`.

This record predates versioned Plan artifacts. Migration 3 preserves the
Intent/Spec history and cancels its pre-versioned `GENERATE_PLAN` placeholder.

The reviewed Spec revision is available from the local Enzo artifact viewer at
<http://localhost:8000/artifacts/b6bf1d38-796e-4951-a324-f2d7f53ecb59>.

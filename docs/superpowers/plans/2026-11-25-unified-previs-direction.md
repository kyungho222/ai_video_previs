# Unified previs direction implementation plan

**Goal:** One model-authored sequence drives both action and camera, with Python allocating the requested duration.

**Architecture:** Add a direction contract containing ordered beats. Each beat owns its performers, actions, camera framing, relative weight and optional explicit duration. Derive camera subjects from the beat rather than accepting an independent focus list. Compile to the existing server-owned scene format; retain legacy scene loading and bounded completion behavior.

**Scope:** Work in the existing local checkout because the running app depends on its accumulated changes. Do not modify correction policy or add paid generation calls. Scenario cards group beats instead of limiting camera intervals.

- [x] Add regression tests for sequential performers, group finale, explicit waits, duration allocation and invalid references.
- [x] Implement direction schema, batched validation and deterministic conversion to existing tracks/scenes.
- [x] Switch model planning to the unified contract; persist direction for revisions; preserve legacy scene compatibility and completion fallback.
- [x] Exercise planning-to-compiler integration and existing relevant tests; inspect the generated camera schedule; restart the idle local server and verify health.

**Validation limits:** Offline tests can prove generated schedules and Blender code compilation, but cannot prove real model adherence or visual quality without a paid generation.

**Verification:** 86 relevant tests passed. Full build test checks the actual generated server Python: male 1, female, male 2, then the whole group, with synchronized action/camera intervals. Fresh-context review found an explicit-duration preservation issue in completion fallback; regression test reproduced it and the fix passed. No remaining material review findings. Existing legacy snapshots remain readable; new AI output must use beats. No additional model calls or correction-policy changes introduced.

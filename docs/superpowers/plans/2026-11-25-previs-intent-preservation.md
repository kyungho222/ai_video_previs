# Previs intent preservation implementation plan

User approved the improvement proposal in this conversation (적용). Execute inline in the existing dirty checkout; preserve unrelated changes. No paid model or render calls for verification.

Goal: preserve authored camera cuts, track source evidence and requested performance coverage, report mismatches without stopping render, remove runtime AI redesign.

- [x] Add regression tests for cuts versus continuous transitions, explicit timing evidence, batched coverage and unknown semantic status, runtime no-retry.
- [x] Extend direction data with transition and source evidence, request requirements linked to beats. Keep legacy stored scenes readable. Source quotes must exist in the original request; unverified timing becomes relative allocation.
- [x] Execute cuts without smoothing or bounds interpolation, retain continuous camera path for continuous shots. Measure framing, fixed-camera drift and action overlap; aggregate warnings.
- [x] Preserve quality status through source/archive and show result warning in the existing UI. No unconditional full-intent pass claim.
- [x] Remove runtime AI redesign; recoverable warnings render once, unrenderable operations retain errors.
- [x] Run focused and integration tests, compile and diff checks; restart local service after confirming no active generation.

Acceptance: separate male/female/male close-up beats then group finale; cuts hit desired camera immediately, continuous movement remains continuous; missed or unmeasured intent is visible; no extra AI correction call.


Implementation notes:
- New strict response schema requires camera transition, directionEvidence, timingEvidence and compact source-linked requirements. Existing stored directions/scenes remain accepted; sourceBeat is Python-owned and never requested from the model.
- Explicit source-backed transition/framing constraints override generated defaults; unverified viewpoint defaults to frontal, unverified durations become relative, unrequested waits do not reserve idle time. Prior user context remains an evidence source on revisions.
- Cuts reset pose smoothing and fit each fixed shot independently; continuous transitions retain smoothing. Actual subject bounds, rather than interpolated bounds, are measured for quality. Close framing requires >=50% extent for its region of interest.
- Coverage audits actor actions, intervals, camera parameters and missing objects. Measurable camera requirements use every linked interval; semantic interpretation of quotes, appearance, dialogue and setting stays unknown.
- Runtime warnings render without AI redesign. Unrenderable remote operations still error. Existing model transport retry/fallback is unchanged.
- Quality and raw direction persist in archive/source/history and completion/reloaded result details. No changes to session-id persistence or scenario generation.
- Verification: focused and integration suite, Python compile, JS syntax, diff checks. Mocked actual camera loop proves wide-to-close cut on first frame, smooth continuous branch, independent repeated cut envelopes after actor relocation, actual-bounds warnings during interpolation. No paid generation, no new rendered-video QA; Blender is unavailable locally.

Final verification: 134 tests passed; Python compile, JavaScript syntax and git diff --check passed. Local server restarted with no active generation; health/console 200 and Jutsu connected (PID 98072). Turning actors use the entry yaw for default frontal view; continued frontal view during rotation is explicitly unverified in result details.

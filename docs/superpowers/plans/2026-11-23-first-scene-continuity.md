# First-scene continuity implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan in order; perform one fresh-context review before completion.

**Goal:** Anchor the opening scene while treating later scene staging as guidance, then generate new A/B videos to inspect transitions.

**Architecture:** Add the explicit `text_continuity` video reference mode. It sends asset and chronological guide images as references, not mandatory endpoint frames; a versioned policy follows optimization, review and prompt fitting. Existing API auto/default and saved jobs retain their endpoint behavior; the console selects the new mode by default.

**Tech stack:** Existing Python/FastAPI/Wan adapter and JavaScript console; no new dependencies.

**Spec:** User-approved discussion in this chat: preserve scene 1, use later scenes for action/dialogue/story intent, carry physical and camera state between scenes; retain explicit cuts, location/time changes, contacts and critical events.

## Constraints and review focus

- Do not change stored source scenes or existing job/provider IDs.
- Later image positions/poses are not mandatory resets; preserve cast, dialogue, event count and timing.
- Preserve explicit cuts and location/time changes; do not infer cuts from scene numbering.
- Retain the legacy first/last mode for A/B and old request retries.
- Missing/unverified first guide must not silently become an arbitrary later opening anchor.
- Preserve the policy during prompt fitting and output review; cached results must match the policy.
- Live generations are newly authorized tests; record task IDs and compare stochastic results without claiming statistical improvement.

## Tasks

- [x] Write failing tests for mode routing/retry, guide roles, temporal carry-over and policy retention.
- [x] Add policy and wire reference-mode preparation, timeline, review and console selection.
- [x] Run focused regression tests, then broader tests only as needed.
- [x] Execute bounded fresh multi-scene A/B trials and retain successes and failures.
- [x] Report that B outputs were blocked; no empirical improvement claim is possible.

## Execution notes

- Work in the active checkout so the requested local-server test uses the edited source. Preserve the pending console model/default changes.
- Baseline known failures: local OAuth SQLite expectation and permanent-retention arithmetic. They are unrelated to this change.
- Ruling: retain the legacy UI default after real tests did not produce a verified B output. The new mode is explicitly selectable as a test mode, not a successful rollout.
- Review fixed policy loss during timeline compaction, incompatible endpoint inputs, and accidental loss of later contact/prop state.
- Focused verification: 36 passed. Earlier full suite: 333 passed, the same two baseline failures.
- Live trial 1: fresh previs cf112344-fc76-464f-bd02-29f1a5ceb121; A 049c23d2-5260-4765-b408-7986846ac874 completed; B d9f2dd0c-ec5f-4c8c-a9c0-0e748eb50190 failed opening-guide verification before video submission.
- Live trial 2: fresh previs 340fd81a-da7e-4d7f-8e06-df96c44ad36d; A e9ad0755-0304-42bc-8705-a7c06e7169e3 completed; B 981bc218-1053-4621-86eb-d9783311b1d0 stopped after opening guide exclusion, with no video provider task submitted.
- The second B job was stopped through FileVideoJobs.update with its live owner and lease, preserving its metadata and invalidating ownership to prevent further guide requests.
- Added fail-fast progress handling so future opening exclusions stop later image work automatically.
- Source inspection found malformed human scaling ([0.5,1.94,0.5] and [1,1.94,0.5]); this confounds pose fidelity. No anatomy/runtime changes made in this continuity task.
- Detailed evidence and baseline videos: ai_video/runtime/local-state/scene-flow-ab/test-report.json and round2/A.mp4. These runtime artifacts are intentionally not versioned.
- No production deployment or Git push performed.

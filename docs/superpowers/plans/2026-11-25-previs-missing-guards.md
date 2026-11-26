# Previs missing guards

Close omissions proven by local job e7628312-d34d-4977-939f-57d47a89520f without enabling AI redesign.

- Test real HTTP admission and cached pipeline admission. Both use one joint classification; moderation stays separate.
- Add performer/observer roles and speaking action. Focus follows performers, never idle observers.
- Remove untimed idle introductions before duration allocation; preserve explicit waits and log adjustments.
- Keep the design call limit at one, including malformed-data completion.
- Add upright blackboard geometry and bounded dimension repair for flattened blackboards.
- Verify offline, review, restart idle server. No paid generation in verification.

Completed: API-to-pipeline test proves one joint admission and cache reuse. Action roles distinguish speaker, performer and observer; speaker idle becomes visible speaking gestures, except explicit waits. Untimed static introduction framing is folded into the first active beat, retaining card intent without extra idle time and keeping active performers/partners visible. Identical current-request history entries and redundant action enum removed from planning payload. Blackboard catalogue and flattened-axis normalization added.

Verification: 113 related tests passed; changed modules compile and diff whitespace checks pass. Fresh review findings (explicit wait precedence, introduction-card preservation, opening shot performer coverage) each have regressions and fixes. Design attempt limit remains 1. No paid model/video generation performed; visual outcome and actual token savings await a new run.

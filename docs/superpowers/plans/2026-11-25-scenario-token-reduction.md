# Scenario token reduction

Recent scenario: 2,300 tokens, 1,846 input + 454 output, two model calls. Preserve input classification/moderation and fresh generation.

- Compact scenario-only default direction instructions and the trust-boundary text for text-only scenario/admission schemas. Retain policy semantics, user dialogue, explicit camera override and Korean-setting defaults.
- Cache successful admission decisions for identical title/summary/model for 300 seconds, per router, bounded to 128 entries. Cache no output scenario or receipt usage. Version checks remain in filter_user_prompts.
- Test fresh generation on cache hits, changed input/model/expiry, blocked decisions, usage attribution and guard idempotence. Report static text reduction separately from actual provider token savings.
- Restart idle local server after verification. No paid generation for benchmarking.

Completed: changed fixed instruction text 3,051 -> 1,866 characters (38.8% of that text; not total request tokens). First-time inputs retain admission + generation. Identical title/summary/model reuses admission for 300 seconds from the original check, never extending on hits; scenario output is always generated fresh. Cached receipts are stripped.

Validation: 100 related tests passed, syntax and diff whitespace checks passed; focused review found no material issues. No paid A/B generation performed, so actual token savings are not measured. Local server restarted after verifying no active jobs.

# Stage usage registration

- Keep usageLogEnabled opt-in (default false). Permit explicit db_name dev_user or aniestkh only; no env database fallback.
- Add stage_usage.py: deduplicate call IDs; aggregate only identical provider/model/modality/billing_unit within a stage job. Multiple models create additional rows beyond three. Preserve individual calls and partial-metering information in raw_usage. Deterministic receipt per group prevents replay inserts.
- Scenario: allocate independent job_id; collect usage locally and submit grouped rows once at terminal completion, including failures. Persist job_id with scenario and expose in response.
- Previs: retain existing per-call events/history JSON; submit groups from full terminal event snapshot, not during each call.
- Video: retain provider, preparation/review LLM and guide-image usage in job metadata; merge receipts across whole metadata updates. Submit grouped rows after local terminal completion, never while a recoverable provider job is still running. Rotate bounded delivery batches to prevent a failed database blocking the other allowed database.
- Keep registration db_name from the current request independently of inherited model workSettings. Empty or disallowed input cannot inherit a permitted DB for registration.
- No changes to History display, user prompts, model selection or generation behavior. No live generation or external DB writes for tests.
- Tests: group boundaries, mixed models/units, retries, token sums, nulls, opt-in/database allowlist, scenario multi-call completion, previs completion, video terminal delivery; existing history and usage regression suites.

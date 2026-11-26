# Previs error recovery

User requirement: retain every detected error and continue generation whenever a meaningful scene can still be rendered. Record original/allowed/replacement values and normalize unambiguous spelling differences locally.

Implementation: preserve the strict adapter for diagnostics; add local bounded recovery at the planning boundary. Preserve original response, collect all contract issues, apply camera aliases before defaults, compile usable subjects/beats, and retain explicit warnings for any loss. Relation correction failures retain the valid pre-correction scene. No new model redesign calls. No subjective numeric quality score is invented: missing usable subjects/beats and unexecutable remote operations remain fatal.

- [x] Reproduce enum failures and absent diagnostic values with regression tests.
- [x] Add enum/type diagnostics and unambiguous camera aliases.
- [x] Recover locally and continue after recoverable relation failures.
- [x] Persist validation records in archives and surface warnings in quality results.
- [x] Complete regression tests, review and local server activation.

Verification: focused 100 tests passed. Full tests directory: 414 passed, two failures outside this change: `test_cut_camera_executes_at_target_on_first_frame` (existing runtime test harness lacks `scene_visibility_roles`) and `test_missing_background_is_identified_for_repair` (existing expected mapping omits newly returned name/sourceIndex). Bare pytest cannot collect a restricted runtime temporary directory. Full tests directory was rerun with temporary-file permissions restored. Review caught and corrected simultaneous channel loss and meaningless placeholder rendering, both with failing-then-passing regressions. Server restarted with no active history/WAN jobs; new PID 5168. No paid generation was performed.

Existing unrelated workspace edits are preserved. New paid generation is outside verification scope.

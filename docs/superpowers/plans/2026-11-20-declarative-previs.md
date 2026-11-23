# Declarative previs implementation plan

Goal: Keep the selected model responsible for scene intent; make Python own geometry,
animation, camera fitting, remote project identity, rendering and artifact retrieval.

Approved scope: the user's AI/Python responsibility table in this conversation.
The default JutsuAgent path changes; previous free-code modules remain available for
saved-code inspection and compatibility tests, but are not called by new jobs.

1. Define a bounded JSON scene contract and validate identifiers, finite coordinates,
   timing, references and supported actions. Preserve the existing plan/timeline contract
   used by downstream scenario snapshots. Test invalid and multi-character plans first.
2. Implement deterministic action sampling and a packaged Blender runtime. Model text is
   data only, never Python source. Build articulated human/animal proxies and props;
   frame evaluated subject geometry with continuous camera interpolation.
3. Add a strict selected-model request option. Validate/repair JSON with the same model,
   with no cross-model fallback. Keep the separate safety moderation service.
4. Implement server-owned create/read/build/validate/render/retrieve sequencing, reuse one
   project during repair, and reconcile uncertain creates by a unique project name.
5. Switch the default agent path, document representation limits, run unit/integration
   tests, and render a local-server smoke job if the local service can be safely reloaded.

Validation: malformed IDs/NaN/timelines, action boundaries, multiple actors, no executable
model code, strict model retention, uncertain create without duplicate creation, same-project
repair, and existing downstream snapshot/media response compatibility. No blanket claim
that every natural-language choreography can be reproduced exactly by a finite action set.


Implementation notes:
- All five implementation steps are complete in source; the default JutsuAgent now uses
  scene_pipeline. Existing free-code modules remain inactive for new jobs.
- Gemma's real strict-schema response repeatedly stopped with finish_reason=length at
  8192 tokens, consisting of partial JSON followed by whitespace. Fixed-size vectors did
  not solve it. Added explicit Gemma schema_mode=json_object with the same local validator.
- Approved synthetic Gemma test: two people, one waves; one successful request, 2318 total
  tokens, google/gemma-4-26B-A4B-it only. Rendered actual 5s MP4 with local Blender 5.2.
- Blender integration checks: independent actor motion, multi-scene locked camera,
  fixed-origin pan, wall occlusion; optional focus schedule preserves legacy behavior.
- Review found wall props were exempt from occlusion and pan executed as orbit; fixed both.
  A follow-up review found cross-scene visibility used a union; fixed with active intervals.
- No live Higgsfield create/render was invoked by the new implementation verification.
  Lifecycle sequencing, uncertain create reconciliation and project reuse were mocked.

Final verification:
- Full pytest: 308 passed, 2 existing failures (OAuth SQLite expectation and legacy expiry
  arithmetic expecting finite RETENTION_SECONDS despite permanent retention).
- New and existing focused contract/provider/snapshot tests passed; actual Blender integration passed.
- Restarted idle local uvicorn only; new PID 129660, /health returns 200 with status=ok.
- No production deployment performed. Local Gemma-derived video is in
  ai_video/runtime/local-state/declarative-review/gemma/previs.mp4.

Follow-up: local scenario HTTP503 after restart (2026-11-20):
- Reproduced Gemma injection classifier echoing JSON schema instead of actual decision values.
- Merge task and JSON contract into one system instruction for explicit json_object routes;
  request JSON instances, not schema definitions. Correct malformed decisions once using the
  same model, remain fail-closed if malformed. Scenario generation also disables GPT fallback.
- Actual synthetic local scenario API: HTTP200, three scenes, Gemma-only usage.
- Actual live local previs job 53805a6d-2edb-4a80-a3f8-80a3e4624c18 completed in ~105s;
  Gemma-only usage, one remote project, MP4 range endpoint206 video/mp4.
- Remaining spatial_manifest warning: upstream refuses application/json artifacts; MP4 successful.
- Full pytest:311 passed, same2 unrelated failures. Restarted idle local server PID71244.

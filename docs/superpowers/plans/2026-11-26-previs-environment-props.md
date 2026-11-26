# Previs environment and prop implementation plan

Implement the requested Python engine improvements without extra model calls.

1. Add regression tests for stage height axes, realistic generated prop dimensions,
   explicit neon/graffiti objects, negative mentions, and preservation of authored objects.
2. Extend the reusable primitive catalog with stage equipment and environment details.
3. Separate prop dimensions and bounded initial placement into `prop_layout.py`.
   Add only requested props; classroom/studio may receive an open rear backdrop,
   avoiding enclosing walls around moving cameras. Keep existing IDs and positions.
4. Correct clearly swapped stage thickness/height axes, except explicit upright intent.
5. Preserve diagnostic records, run focused and adjacent tests, replay saved designs
   offline. Do not claim visual quality until an actual Blender render is inspected.

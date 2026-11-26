"""Single provider-neutral policy; video_prompt re-exports legacy names."""
from .character_asset_policy import SINGLE_CHARACTER_RULE
from .audio_policy import VOICE_PERFORMANCE_RULES

PREVIS_RULES = 'PREVIS: Preserve camera path, viewpoint, shot size, speed, timing, character root positions, routes, formation and major event order. Reconstruct detailed articulation naturally; proxy poses are not performance. Latest user edits win.'

CAMERA_GUIDE_RULES = 'Camera/staging markers define camera and body paths only. Rebuild complete characters and props from assets and scene text; never copy marker shapes or limb poses.'

INDEPENDENT_REACTION_RULES = 'Characters react independently and causally. Preserve explicit synchronization, stillness and event counts; never repeat an action without a request.'

ASSET_REBUILD_RULES = 'Replace proxy characters, backgrounds and props completely with their matching assets. Keep identity, anatomy, wardrobe, style and object count through all viewpoints; no mixed proxy shapes.'
ASSET_REBUILD_RULES += ' '+SINGLE_CHARACTER_RULE

CONTINUITY_RULES = 'Maintain connected space, persistent objects, contact states, travel direction and camera momentum across action beats. Reach the final composition through continuous motion, not a pose reset or abrupt snap. Honor explicit cuts/time jumps.'

MOTION_RULES = 'GENERATION: Latest permitted user requirements win. Use asset-defined identity, materials and style; preserve explicit animation styles. Keep every body connected with consistent limb count, anatomy and proportions. Coordinate head, torso, pelvis and limbs during turns and occlusion. Generate natural weight transfer, foot support, hand/object contact, anticipation and follow-through around the established root paths. Preserve required actions, targets, facing, event counts and timing. Do not invent people, objects, plot events or text. Reference sheets are not scene objects.'

SPEECH_LANGUAGE_RULES = "VOICE/AUDIO: Generate only requested voices; singing alone does not authorize backing music. Add music/effects only when requested; preserve silence and supplied audio. Explicit speaker language wins; otherwise cartoon/stylized and explicitly East Asian characters use Korean, explicitly foreign/non-East-Asian humans English, unspecified Korean. Never infer nationality from appearance. Assign persistent distinct voices and lip sync to named speakers; allow requested or shared ensemble overlaps/unison. Preserve exact dialogue meaning and each speaker's turn. Requested music defaults to fitting Korean style unless specified otherwise."
SPEECH_LANGUAGE_RULES += "\n" + VOICE_PERFORMANCE_RULES

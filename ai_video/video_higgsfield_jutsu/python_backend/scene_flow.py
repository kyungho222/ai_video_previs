"""Reference priority for continuous action across planned scene boundaries."""

FIRST_SCENE_CONTINUITY = "first-scene-continuity-v1"

SCENE_FLOW_RULES = """FIRST SCENE ANCHOR AND CONTINUOUS SCENE FLOW
Use scene 1's opening composition as the initial physical state: cast identities, positions, facing, spacing, environment and camera.
Scene 2 and later images/timeline observations are intent references for actions, dialogue, interactions and shot goals, not mandatory absolute positions, poses or camera resets. A scene number alone never requests a cut.
For consecutive scenes in the same space and time, inherit the preceding scene's actual final position, pose, facing, gaze, prop/contact state, travel velocity and camera momentum. Adapt later staging and camera travel to reach the requested shot goals through visible continuous motion; never teleport, snap to a reference pose, restart a walk cycle or duplicate an event to match an image.
Preserve assigned speakers and exact dialogue, event order/counts, required contacts/targets, critical event times and explicitly requested camera constraints. Honor explicit cuts, location changes and time jumps; do not force a continuous take across them.
Within continuous scenes this priority overrides generic instructions to copy later reference root coordinates, formations, endpoint compositions or camera paths. Explicit current user requirements remain highest priority."""


def continuous_guides(guides):
    """Never substitute a later scene after losing the opening reference."""
    ordered = sorted(guides, key=lambda guide: guide["index"])
    if not ordered or ordered[0]["index"] != 0 or ordered[0].get("status") != "verified":
        raise ValueError("A verified opening guide is required for continuous scene flow")
    return [guide for guide in ordered if guide.get("status") == "verified"]

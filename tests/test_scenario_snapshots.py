import pytest

from ai_video.video_higgsfield_jutsu.python_backend.previs_workers import validate_plan
from ai_video.video_higgsfield_jutsu.python_backend.scenario_snapshots import (
    capture_scenario_snapshots, limit_scenario_frames, scenario_frames,
)
from ai_video.video_higgsfield_jutsu.python_backend.store import JobStore


def test_scenario_snapshots_follow_previs_timeline_and_card_order(tmp_path):
    titles = ["준비", "계기", "추격", "마무리"]
    store = JobStore(tmp_path)
    job, _, _ = store.create("시나리오", duration=10, aspect="4:3", scenario_titles=titles)
    saved = store.get(job["id"])["data"]
    assert saved["scenarioTitles"] == titles
    assert (saved["duration"], saved["aspect"]) == (10, "4:3")
    intervals = [(0, 2), (2, 3), (3, 8), (8, 10)]
    plan = {
        "scene_action": "고양이와 나비", "camera": "고정 카메라", "motion_detail": "basic_motion",
        "timeline": [
            {"start": start, "end": end, "subject_action": title, "camera_goal": "고정"}
            for title, (start, end) in zip(titles, intervals)
        ],
    }
    with pytest.raises(ValueError, match="exactly 3"):
        validate_plan(plan, 10, 3)
    assert scenario_frames({}) == []
    scenes = scenario_frames({**saved, "plan": plan})
    assert [scene["title"] for scene in scenes] == titles
    assert [scene["fraction"] for scene in scenes] == [0.1, 0.25, 0.55, 0.9]
    assert [scene["index"] for scene in limit_scenario_frames(scenes, 2)] == [0, 3]

    cv2 = pytest.importorskip("cv2")
    import numpy as np

    mp4 = tmp_path / "previs.mp4"
    writer = cv2.VideoWriter(str(mp4), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
    try:
        assert writer.isOpened()
        for frame in range(100):
            writer.write(np.full((64, 64, 3), frame, dtype=np.uint8))
    finally:
        writer.release()
    snapshots = capture_scenario_snapshots(mp4, tmp_path / "snapshots", scenes)
    assert [entry["frame"] for entry, _ in snapshots] == [10, 25, 55, 90]
    assert [entry["time"] for entry, _ in snapshots] == [1, 2.5, 5.5, 9]
    assert all(cv2.imread(str(path)) is not None for _, path in snapshots)

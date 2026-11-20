"""Map the ordered scenario cards to representative frames of the previs."""

from .previs_workers import validate_plan
from .spatial_guides import sample_frames


def scenario_frames(source_data):
    titles = source_data.get("scenarioTitles")
    if not titles:
        return []
    plan = source_data.get("plan") or {}
    timeline = plan.get("timeline") or []
    if not timeline:
        raise ValueError("시나리오 씬 타임라인이 없습니다.")
    duration = timeline[-1]["end"]
    validate_plan(plan, duration, len(titles))
    return [
        {"index": index, "title": title, "fraction": (event["start"] + event["end"]) / (2 * duration)}
        for index, (title, event) in enumerate(zip(titles, timeline))
    ]


def capture_scenario_snapshots(reference, directory, scenes):
    directory.mkdir(parents=True, exist_ok=True)
    frames = sample_frames(
        reference, directory, len(scenes), "references",
        frame_fractions=[scene["fraction"] for scene in scenes],
    )
    return [
        ({**scene, "frame": frame["frame"], "time": frame["time"]}, frame["path"])
        for scene, frame in zip(scenes, frames)
    ]


def limit_scenario_frames(scenes, count):
    if len(scenes) <= count:
        return scenes
    if count == 1:
        return [scenes[len(scenes) // 2]]
    return [scenes[round(index * (len(scenes) - 1) / (count - 1))] for index in range(count)]

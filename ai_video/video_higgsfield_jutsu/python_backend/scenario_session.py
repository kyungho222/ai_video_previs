"""Persist scenario context for later previs requests."""

import json
from pathlib import Path
from uuid import UUID, uuid4


class ScenarioSessions:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, session_id):
        return self.directory / (str(UUID(session_id)) + ".json")

    def save(self, record, session_id=None):
        session_id = session_id or str(uuid4())
        path = self.path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + str(uuid4()) + ".tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return session_id

    def get(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))


def scenario_prompt(record):
    sections = [
        "아래 장면 순서대로 숏폼 프리비즈를 만들어 주세요.",
        "제목: " + record["title"],
        "개요: " + record["summary"],
    ]
    sections.extend(
        f"{index}. {scene['title']}\n{scene['prompt']}"
        for index, scene in enumerate(record["scenes"], 1)
    )
    return "\n\n".join(sections)

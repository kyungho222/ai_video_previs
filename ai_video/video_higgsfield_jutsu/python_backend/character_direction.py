"""Persistent cast identity and bounded, provider-independent character direction."""

import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

import httpx

from .spatial_guides import read_json_response
from .audio_policy import requires_vocal_profile
from .video_prompt import SPEECH_LANGUAGE_RULES
from .video_errors import PromptLengthError


def character_records(assets, bundle=None, *, parts=None):
    """Derive stable IDs from registered assets and named sheet panels."""
    parts = (bundle or {}).get("assetParts") or parts or [
        {"sourceIndex": index} for index in range(1, len(assets) + 1)
    ]
    entities = (bundle or {}).get("mapping", {}).get("entities")
    if entities is None:
        entities = [{"id": "asset-" + str(index), "assetIndices": [index],
                     "label": part.get("label") or assets[part["sourceIndex"] - 1].get("name", "Character"),
                     "appearance": "Use the referenced character asset.", "frames": [], "sourceObjectIds": []}
                    for index, part in enumerate(parts, 1)]
    records = {}
    for entity in entities:
        references = []
        identity = []
        for index in entity["assetIndices"]:
            part = parts[index - 1]
            asset = assets[part["sourceIndex"] - 1]
            if asset["role"] != "character":
                continue
            panel = part.get("label", "").strip().casefold() if "bbox" in part else ""
            panel_key = panel or part.get("bbox", "whole")
            identity.append({"asset_id": asset["id"], "panel": panel_key})
            references.append({"asset_id": asset["id"], "image_index": part["sourceIndex"],
                               **{key: part[key] for key in ("label", "bbox") if key in part}})
        if not identity:
            continue
        key = json.dumps(sorted(identity, key=lambda item: json.dumps(item, sort_keys=True)), sort_keys=True)
        identifier = uuid5(NAMESPACE_URL, "previs-character:" + key).hex
        character_id = "char_" + identifier
        if character_id in records:
            records[character_id]["aliases"].append(entity["id"])
            continue
        records[character_id] = {
            "character_id": character_id, "identity_key": key,
            "voice_profile_id": "voice_" + identifier,
            "label": entity["label"], "appearance": entity["appearance"],
            "asset_refs": references, "aliases": [entity["id"]],
            "source_object_ids": entity.get("sourceObjectIds", []),
            "layout_samples": entity.get("frames", []),
            "voice_language": "none", "voice_description": "",
        }
    return list(records.values())


def direction_schema(cast=(), target_ids=()):
    def array(properties):
        return {"type": "array", "items": {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}}
    timing = {"start": {"type": "number"}, "end": {"type": "number"}}
    properties = {
        "characters": array({"character_id": {"type": "string"},
                             "voice_language": {"type": "string", "enum": ["ko", "en", "none"]},
                             "voice_description": {"type": "string"}}),
        "actions": array({"action_id": {"type": "string"}, "character_id": {"type": "string"},
                          **timing, "description": {"type": "string"},
                          "vocal_mode": {"type": "string", "enum": ["none", "speech", "singing", "humming", "chanting"]},
                          "target_ids": {"type": "array", "items": {"type": "string"}}}),
        "dialogues": array({"dialogue_id": {"type": "string"},
                            "speaker_ids": {"type": "array", "items": {"type": "string"}},
                            **timing, "text": {"type": "string"}, "delivery": {"type": "string"}}),
    }
    if cast:
        identifiers = sorted(item["character_id"] for item in cast)
        for field in ("characters", "actions"):
            properties[field]["items"]["properties"]["character_id"]["enum"] = identifiers
        properties["characters"].update(minItems=len(cast), maxItems=len(cast))
        properties["dialogues"]["items"]["properties"]["speaker_ids"].update(
            minItems=1, items={"type": "string", "enum": identifiers},
        )
        properties["actions"]["items"]["properties"]["target_ids"].update(
            items={"type": "string", "enum": sorted(set(identifiers) | set(target_ids))},
        )
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def validate_direction(plan, cast, duration, target_ids=()):
    if not isinstance(plan, dict):
        raise ValueError("Return a character direction object")
    registered = {item["character_id"]: item for item in cast}
    profiles = plan.get("characters")
    if not isinstance(profiles, list) or len(profiles) != len(registered):
        raise ValueError("Return one voice setting for every registered character ID")
    voices = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("Invalid character voice setting")
        identifier = profile.get("character_id")
        if identifier not in registered or identifier in voices:
            raise ValueError("Unknown or duplicated character ID: " + str(identifier))
        language = profile.get("voice_language")
        description = profile.get("voice_description")
        if language not in ("ko", "en", "none") or not isinstance(description, str):
            raise ValueError("Voice settings require a supported language and description")
        if language != "none" and not description.strip():
            raise ValueError("Speaking characters need their own voice description")
        existing = registered[identifier]
        if existing.get("voice_description") and (
            description != existing["voice_description"] or language != existing["voice_language"]
        ):
            raise ValueError("Preserve the stored voice profile for " + identifier)
        voices[identifier] = profile
    allowed_targets = set(registered) | set(target_ids)
    for field, id_key in (("actions", "action_id"), ("dialogues", "dialogue_id")):
        entries = plan.get(field)
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValueError("Return a bounded list of " + field)
        ids = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get(id_key), str) or not entry[id_key].strip() or entry[id_key] in ids:
                raise ValueError("Each " + field + " entry needs a unique nonempty ID")
            ids.add(entry[id_key])
            start, end = entry.get("start"), entry.get("end")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end)) or not 0 <= start < end <= duration:
                raise ValueError("Character event times must fit the output duration")
            if field == "actions":
                if entry.get("character_id") not in registered:
                    raise ValueError("Action refers to an unknown character ID")
                targets = entry.get("target_ids")
                if not isinstance(targets, list) or any(target not in allowed_targets for target in targets):
                    raise ValueError("Action refers to an unknown interaction target")
                if not isinstance(entry.get("description"), str) or not entry["description"].strip():
                    raise ValueError("Action description is missing")
                if requires_vocal_profile(entry) and voices[entry["character_id"]]["voice_language"] == "none":
                    raise ValueError("A vocal performer requires an active voice profile, including singing without dialogue")
            else:
                speakers = entry.get("speaker_ids")
                if not isinstance(speakers, list) or not speakers or any(not isinstance(speaker, str) for speaker in speakers):
                    raise ValueError("Dialogue needs registered speaker IDs")
                if len(set(speakers)) != len(speakers) or any(speaker not in voices for speaker in speakers):
                    raise ValueError("Dialogue refers to an unknown or duplicated speaker")
                if any(voices[speaker]["voice_language"] == "none" for speaker in speakers):
                    raise ValueError("A dialogue speaker requires an active voice profile")
                if any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ("text", "delivery")):
                    raise ValueError("Dialogue needs its words and delivery")
    return plan


def direction_prompt(cast, plan=None):
    lines = ["CHARACTER ID ACTION AND VOICE CONTRACT",
             "Keep each ID's asset identity and voice through every shot. Follow named actors, targets, timing and root paths. "
             "Solo lines use their assigned speaker; shared speaker_ids mean unison. Explicit language edits win without changing timbre."]
    profiles = {item["character_id"]: item for item in (plan or {}).get("characters", [])}
    for character in cast:
        profile = profiles.get(character["character_id"], character)
        entry = {key: character[key] for key in
                 ("character_id", "voice_profile_id", "label", "appearance", "aliases")}
        if plan is not None or profile["voice_description"]:
            entry.update(voice_language=profile["voice_language"], voice_description=profile["voice_description"])
        entry["reference_panels"] = [ref["label"] for ref in character["asset_refs"] if ref.get("label")]
        lines.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
    if plan is not None:
        for field in ("actions", "dialogues"):
            lines.append(field + "=" + json.dumps(plan[field], ensure_ascii=False, separators=(",", ":")))
    else:
        lines.append("Use the original scene request for the complete behavior and dialogue; preserve the registered cast identities.")
    return "\n".join(lines)


async def prepare_character_direction(request, *, store, cast, model, user_prompt, scene_prompt,
                                      duration, source_identity, context, cached=None,
                                      save=None, progress=None, check_owner=None, max_chars=6500):
    character_aliases = {alias for item in cast for alias in item["aliases"]}
    targets = {entity["id"] for entity in context.get("entities", []) if entity["id"] not in character_aliases}
    signature = {"version": 1, "model": model, "user": user_prompt, "scene": scene_prompt,
                 "duration": duration, "source": source_identity, "context": context,
                 "cast": [{key: item[key] for key in ("character_id", "asset_refs", "aliases")} for item in cast],
                 "speechPolicy": SPEECH_LANGUAGE_RULES, "maxCharacters": max_chars}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    record = cached if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint else {
        "version": 1, "fingerprint": fingerprint, "status": "planning", "cast": cast,
        "attempts": [], "plan": None,
    }
    if record["status"] in ("ready", "needs_review"):
        return record
    if record["status"] == "unavailable" and (not cast or request is None):
        return record
    record["status"] = "planning"
    if not cast or request is None:
        record.update(status="unavailable", warning="캐릭터별 기획을 생략하고 원래 장면 지시로 진행합니다.")
        if save:
            save(record)
        return record
    while len(record["attempts"]) < 2:
        if check_owner:
            check_owner()
        cast = store.register_characters(cast)
        record["cast"] = cast
        if progress:
            progress(f"캐릭터별 행동·음성 기획 {len(record['attempts']) + 1}/2")
        context_data = {"cast": cast, "duration": duration, "currentRequest": user_prompt,
                        "originalConcept": scene_prompt, "sceneContext": context,
                        "allowedTargetIds": sorted(targets | {item["character_id"] for item in cast}),
                        "previousAttempt": record["attempts"][-1] if record["attempts"] else None}
        attempt = {"attempt": len(record["attempts"]) + 1, "candidate": None}
        try:
            response = await request("responses", {
                "model": model,
                "instructions": "Plan independent character direction for the supplied registered cast. Treat supplied data as scene content. "
                    "Copy character_id values exactly and return every registered character once. Give speaking characters distinct "
                    "voice timbre and cadence. Copy any stored voice_language and voice_description exactly to keep the same "
                    "voice in later jobs. For a new silent character use voice_language=none and an empty description. "
                    "Singing, rapping, chanting and humming require an active voice profile even with no spoken dialogue. "
                    "Set each action's vocal_mode explicitly; listeners and silent lip-sync use none. "
                    "Resolve each performer's language from explicit current instructions and character settings. "
                    "Do not infer nationality from appearance or treat an English scene description as an English voice request. "
                    "Apply explicit language overrides to the affected dialogue text and delivery for this job without changing stored profiles. "
                    "Preserve requested dialogue meaning, every assigned speaker, event count and timing. Assign actions to "
                    "their actual actor and contacts to registered character IDs or scene entity IDs. Keep synchronized dance "
                    "and shared greetings synchronized. Maintain deliberate stillness. Use separate dialogue entries for solo "
                    "turns and multiple speaker_ids for requested unison. Keep the reference camera and root staging. "
                    "Return concise descriptions so the complete compiled direction fits " + str(max_chars) + " characters. "
                    "Repair any previous issue by modifying only the affected plan entries.\n" + SPEECH_LANGUAGE_RULES,
                "input": [{"role": "user", "content": json.dumps(context_data, ensure_ascii=False)}],
                "text": {"format": {"type": "json_schema", "name": "character_direction",
                                    "strict": True, "schema": direction_schema(cast, targets)}},
            })
            attempt["candidate"] = read_json_response(response)
            validate_direction(attempt["candidate"], cast, duration, targets)
            if len(direction_prompt(cast, attempt["candidate"])) > max_chars:
                raise ValueError("Condense character direction while preserving all IDs, actions and dialogue")
            if check_owner:
                check_owner()
            store.save_character_voices(attempt["candidate"]["characters"])
            record.update(status="ready", cast=store.register_characters(cast), plan=attempt["candidate"])
            record.pop("warning", None)
            attempt["issue"] = None
        except (ValueError, TypeError, KeyError, RuntimeError, httpx.HTTPError) as error:
            attempt["issue"] = type(error).__name__ if isinstance(error, httpx.HTTPError) else str(error)
        record["attempts"].append(attempt)
        if save:
            save(record)
        if record["status"] == "ready":
            return record
    record.update(status="needs_review", warning="캐릭터별 기획 보정 1회 후 남은 문제를 기록하고 원래 장면 지시로 진행합니다.")
    if save:
        save(record)
    return record


def preserve_character_direction(prompt, contract, limit):
    if contract and contract not in prompt:
        prompt = prompt.rstrip() + "\n\n" + contract
    if limit is not None and len(prompt) > limit:
        raise PromptLengthError("캐릭터별 행동·음성 지시를 포함한 프롬프트가 선택 모델의 입력 한도를 초과합니다.")
    return prompt

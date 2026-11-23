"""Asset-based still guides, independent of optional motion/video adapters."""

import asyncio
import base64
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import httpx
from PIL import Image, ImageOps
from .video_errors import VideoError
from .video_prompt import PREVIS_RULES
from .scene_flow import FIRST_SCENE_CONTINUITY, SCENE_FLOW_RULES
from .prompt_filter import MODEL_INPUT_GUARD, attach_presentation_rules

GUIDE_VERSION = "spatial-guides-v2"
GUIDE_GENERATION_ATTEMPTS = 3
MAPPING_REPAIR_ATTEMPTS = 3
MAPPING_REGENERATION_ATTEMPTS = 2


class GuideError(VideoError):
    pass


def entity_original_refs(bundle, entity):
    parts = bundle.get("assetParts")
    if parts is None:
        return [{"sourceIndex": index} for index in entity["assetIndices"]]
    return [parts[index - 1] for index in entity["assetIndices"]]


def spatial_prompt_context(bundle, input_format, *, scene_flow_policy=None):
    entities = []
    for entity in bundle["mapping"]["entities"]:
        item = {key: entity[key] for key in ("id", "label", "appearance")}
        if input_format == "references":
            refs = entity_original_refs(bundle, entity)
            item["assetIndices"] = sorted({ref["sourceIndex"] for ref in refs})
            regions = [
                {"imageIndex": ref["sourceIndex"], "label": ref["label"], "bbox": ref["bbox"]}
                for ref in refs
                if "bbox" in ref
            ]
            if regions:
                item["assetRegions"] = regions
        entities.append(item)
    return {
        **({"sceneFlowPolicy": scene_flow_policy, "sceneFlowRules": SCENE_FLOW_RULES}
           if scene_flow_policy == FIRST_SCENE_CONTINUITY else {}),
        "inputFormat": input_format,
        "entities": entities,
        "cameraSummary": bundle["mapping"]["cameraSummary"],
    }


def scene_text_without_asset_indices(text, bundle):
    import re

    labels = {}
    for entity in bundle["mapping"]["entities"]:
        for ref in entity_original_refs(bundle, entity):
            labels.setdefault(ref["sourceIndex"], []).append(entity["label"])
    labels = {index: ", ".join(dict.fromkeys(names)) for index, names in labels.items()}
    return re.sub(
        r"\b(?:Image|Img)\s*(\d+)\b",
        lambda match: labels.get(int(match[1]), "the corresponding character"),
        text,
        flags=re.I,
    )


def bind_spatial_prompt(prompt, bundle, asset_count, input_format, motion_enabled, scene_flow_policy=None):
    lines = [
        "SPATIAL IMAGE AND CAMERA REFERENCE ROLES",
        "Spatial images establish appearance, environment identity and an initial composition. "
        "Maintain the same mapped cast, asset identity and environment across every shot and reappearance. "
        "Follow the reference camera viewpoint progression and camera travel. Preserve character root positions, travel paths/directions, relative formations and major event timing when a previs video is supplied. Independently reconstruct natural detailed articulation and reactions around that staging, respecting explicit current user changes.",
    ]
    if input_format == "references":
        for image_number, guide in enumerate(sorted(bundle["guides"], key=lambda item: item["index"]), asset_count + 1):
            if scene_flow_policy == FIRST_SCENE_CONTINUITY:
                role = "OPENING STATE ANCHOR" if guide["index"] == 0 else "LATER SCENE INTENT REFERENCE"
                lines.append(f"Image {image_number}: {role} near {guide['time']:.2f}s; same cast. Later positions and poses are not mandatory resets.")
                continue
            lines.append(
                f"Image {image_number}: asset-based spatial composition near {guide['time']:.2f}s of the planning reference; same entities, not extra characters. Its pose is not a mandatory motion keyframe."
            )
        for entity in bundle["mapping"]["entities"]:
            refs = ", ".join(
                (
                    f"Image {ref['sourceIndex']} panel {ref['label']} at normalized box {ref['bbox']}"
                    if "bbox" in ref
                    else f"Image {ref['sourceIndex']}"
                )
                for ref in entity_original_refs(bundle, entity)
            )
            lines.append(f"{entity['id']} ({entity['label']}): {refs}; {entity['appearance']}")
    else:
        lines.append(
            "The supplied first frame"
            + (" and last frame" if input_format == "first_last" else "")
            + " defines the visible cast and environment at those instants. Do not force offscreen cast into those frames. Generate natural movement from/between those compositions; no separate asset images or motion video are included."
        )
        for entity in bundle["mapping"]["entities"]:
            lines.append(
                f"{entity['id']} ({entity['label']}): {entity['appearance']}; maintain this identity on every later appearance."
            )
    lines.append("Camera/staging intent: " + bundle["mapping"]["cameraSummary"])
    if motion_enabled and PREVIS_RULES not in prompt:
        lines.append(PREVIS_RULES)
    if scene_flow_policy == FIRST_SCENE_CONTINUITY:
        lines.append(SCENE_FLOW_RULES)
    return prompt + "\n\n" + "\n".join(lines)


def image_data_url(path, max_side=1536):
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((max_side, max_side))
        output = BytesIO()
        image.save(output, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


def read_json_response(response):
    if response.get("status") not in (None, "completed"):
        raise GuideError("공간 가이드 분석 응답이 완료되지 않았습니다.")
    try:
        text = "".join(
            part.get("text", "")
            for item in response.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        return json.loads(text)
    except (ValueError, AttributeError, TypeError):
        raise GuideError("공간 가이드 분석 응답을 해석할 수 없습니다.") from None


def sample_frames(source, directory, count, input_format, *, frame_indices=None, frame_fractions=None):
    import cv2

    if type(count) is not int or not 1 <= count <= 64:
        raise GuideError("공간 가이드는 1~64장으로 지정하세요.")
    count = 1 if input_format == "first_frame" else 2 if input_format == "first_last" else count
    cap = cv2.VideoCapture(str(source))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        if total < 1 or not fps or not math.isfinite(fps):
            raise GuideError("프리비즈에서 공간 가이드 프레임을 읽을 수 없습니다.")
        if frame_fractions is not None:
            frame_indices = [min(total - 1, int(fraction * total)) for fraction in frame_fractions]
        result = []
        if frame_indices is not None:
            count = len(frame_indices)
        for index in range(count):
            frame = (
                frame_indices[index]
                if frame_indices is not None
                else round(index * (total - 1) / (count - 1)) if count > 1 else 0
            )
            if type(frame) is not int or not 0 <= frame < total:
                raise GuideError("선택한 공간 가이드 프레임이 올바르지 않습니다.")
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, pixels = cap.read()
            if not ok:
                raise GuideError("공간 가이드의 원본 프레임을 읽지 못했습니다.")
            path = directory / f"layout-{index}.png"
            if not cv2.imwrite(str(path), pixels):
                raise GuideError("공간 가이드 원본 프레임 저장에 실패했습니다.")
            result.append(
                {
                    "index": index,
                    "frame": frame,
                    "time": round(frame / fps, 4),
                    "path": path,
                    "width": pixels.shape[1],
                    "height": pixels.shape[0],
                }
            )
        return result
    finally:
        cap.release()


SCENE_CRITERIA = (
    "scene_transition",
    "camera_change",
    "character_appearance",
    "staging_change",
    "final_framing",
)
SCENE_REASON_LABELS = {
    "scene_transition": "씬 전환",
    "camera_change": "카메라 변화",
    "character_appearance": "캐릭터 등장·재등장",
    "staging_change": "배치 변화",
    "final_framing": "최종 구도",
}


def spatial_guide_budget(asset_count, requested="auto", *, image_limit=10):
    capacity = image_limit - asset_count
    if capacity < 1:
        raise GuideError(f"원본 에셋과 공간 가이드 합계가 모델 한도 {image_limit}장을 초과합니다.")
    if requested == "auto":
        return capacity
    if type(requested) is not int or not 1 <= requested <= 64 or requested > capacity:
        raise GuideError(f"에셋과 공간 가이드 합계를 모델 한도 {image_limit}장 이내로 설정하세요.")
    return requested


def scene_candidates(source, directory, *, maximum=64):
    """Survey the entire clip; retain temporal coverage plus strong visual-change candidates."""
    import cv2
    import numpy as np

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        if total < 1 or not math.isfinite(fps) or fps <= 0:
            raise GuideError("주요 씬 분석용 프리비즈를 읽을 수 없습니다.")
        # All of the clip is surveyed; bound analysis work for very long uploads.
        step = max(1, round(fps * 0.5), math.ceil(total / 4096))
        changes = []
        previous = None
        for frame in range(0, total, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, pixels = cap.read()
            if not ok:
                continue
            small = cv2.resize(pixels, (160, 90))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
            cv2.normalize(hist, hist)
            if previous is not None:
                score = (
                    float(cv2.compareHist(previous[1], hist, cv2.HISTCMP_BHATTACHARYYA))
                    + 0.5 * float(np.mean(cv2.absdiff(previous[0], gray))) / 255
                )
                if score > 0.12:
                    changes.append((score, frame))
            previous = (gray, hist)
        uniform_count = min(maximum, total, max(3, math.ceil(total / (fps * 0.5)) + 1))
        anchors = (
            {round(i * (total - 1) / (uniform_count - 1)) for i in range(uniform_count)}
            if uniform_count > 1
            else {0}
        )
        if len(anchors) >= maximum:
            anchors = {round(i * (total - 1) / (maximum // 2 - 1)) for i in range(maximum // 2)}
        for score, frame in sorted(changes, reverse=True):
            if len(anchors) >= maximum:
                break
            if all(abs(frame - other) > max(1, fps * 0.15) for other in anchors):
                anchors.add(frame)
        result = []
        for index, frame in enumerate(sorted(anchors)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, pixels = cap.read()
            if not ok:
                raise GuideError("주요 씬 후보 프레임을 읽지 못했습니다.")
            h, w = pixels.shape[:2]
            pixels = cv2.resize(
                pixels,
                (
                    max(1, round(w * min(1, 512 / max(w, h)))),
                    max(1, round(h * min(1, 512 / max(w, h)))),
                ),
            )
            path = directory / f"candidate-{index}.png"
            if not cv2.imwrite(str(path), pixels):
                raise GuideError("주요 씬 후보 저장에 실패했습니다.")
            result.append(
                {"index": index, "frame": frame, "time": round(frame / fps, 4), "path": path}
            )
        return result
    finally:
        cap.release()


SCENE_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidateIndex": {"type": "integer"},
                    "reasons": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SCENE_CRITERIA)},
                    },
                    "label": {"type": "string"},
                    "importance": {"type": "number"},
                },
                "required": ["candidateIndex", "reasons", "label", "importance"],
                "additionalProperties": False,
            },
        },
        "coverageNotes": {"type": "string"},
    },
    "required": ["scenes", "coverageNotes"],
    "additionalProperties": False,
}

CAST_SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "sheets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "imageIndex": {"type": "integer"},
                    "members": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "bbox": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["label", "bbox"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["imageIndex", "members"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sheets"],
    "additionalProperties": False,
}


def character_asset_parts(assets, analysis):
    sheets = analysis.get("sheets")
    characters = {index for index, asset in enumerate(assets, 1) if asset["role"] == "character"}
    if not isinstance(sheets, list):
        raise GuideError("캐릭터 시트 분석 결과가 올바르지 않습니다.")
    members_by_source = {}
    for sheet in sheets:
        index, members = sheet.get("imageIndex"), sheet.get("members")
        if type(index) is not int or index not in characters or index in members_by_source:
            raise GuideError("캐릭터 시트 이미지 번호가 올바르지 않습니다.")
        if not isinstance(members, list) or len(members) == 1 or len(members) > 10:
            raise GuideError(
                "통합 시트는 서로 다른 멤버를 2~10개 영역으로 분리할 수 있어야 합니다."
            )
        boxes = []
        labels = set()
        for member in members:
            box, label = member.get("bbox"), member.get("label")
            if (
                not isinstance(label, str)
                or not label.strip()
                or label.strip() in labels
                or not isinstance(box, list)
                or len(box) != 4
                or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in box)
                or box[2] - box[0] < 0.02
                or box[3] - box[1] < 0.02
            ):
                raise GuideError("통합 시트의 멤버 이름 또는 이미지 영역이 올바르지 않습니다.")
            left, top, right, bottom = box
            for x1, y1, x2, y2 in boxes:
                intersection = max(0, min(right, x2) - max(left, x1)) * max(
                    0, min(bottom, y2) - max(top, y1)
                )
                if intersection > 0.15 * min(
                    (right - left) * (bottom - top), (x2 - x1) * (y2 - y1)
                ):
                    raise GuideError("통합 시트의 멤버 영역이 서로 겹칩니다.")
            labels.add(label.strip())
            boxes.append(box)
        members_by_source[index] = members
    if members_by_source.keys() != characters:
        raise GuideError("일부 캐릭터 시트의 분석 결과가 누락되었습니다.")

    parts = []
    for index in range(1, len(assets) + 1):
        members = members_by_source.get(index)
        if members:
            for member in sorted(members, key=lambda item: (item["bbox"][1], item["bbox"][0])):
                parts.append(
                    {"sourceIndex": index, "label": member["label"].strip(), "bbox": member["bbox"]}
                )
        else:
            parts.append({"sourceIndex": index})
    return parts


def prepare_asset_parts(assets, parts, directory, *, image_constraints=None):
    working = []
    for index, part in enumerate(parts, 1):
        original = assets[part["sourceIndex"] - 1]
        item = {**original, **part}
        if "bbox" in part:
            target = directory / f"asset-part-{index}.png"
            with Image.open(original["path"]) as image:
                image = ImageOps.exif_transpose(image)
                box = tuple(
                    round(value * size)
                    for value, size in zip(part["bbox"], (*image.size, *image.size))
                )
                temporary = target.with_suffix(".part")
                crop = image.crop(box)
                if image_constraints:
                    crop = crop.convert("RGB")
                    width, height = crop.size
                    padded_width = max(width, math.ceil(height / image_constraints["max_aspect"]))
                    padded_height = max(height, math.ceil(width / image_constraints["max_aspect"]))
                    if (padded_width, padded_height) != crop.size:
                        crop = ImageOps.pad(crop, (padded_width, padded_height))
                    crop.thumbnail(
                        (min(2048, image_constraints["max_side"]),) * 2, Image.Resampling.LANCZOS
                    )
                    if min(crop.size) < image_constraints["min_side"]:
                        ratio = image_constraints["min_side"] / min(crop.size)
                        crop = crop.resize(
                            tuple(math.ceil(side * ratio) for side in crop.size),
                            Image.Resampling.LANCZOS,
                        )
                crop.save(temporary, format="PNG")
                temporary.replace(target)
            item["path"] = target
        working.append(item)
    return working


MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "cameraSummary": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "assetIndices": {"type": "array", "items": {"type": "integer"}},
                    "label": {"type": "string"},
                    "appearance": {"type": "string"},
                    "sourceObjectIds": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "frames": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "visible": {"type": "boolean"},
                                "bbox": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["index", "visible", "bbox"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "id",
                    "assetIndices",
                    "label",
                    "appearance",
                    "sourceObjectIds",
                    "confidence",
                    "frames",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["cameraSummary", "entities"],
    "additionalProperties": False,
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        **{
            name: {"type": "boolean"}
            for name in (
                "assetIdentityPreserved",
                "cameraCompositionPreserved",
                "proxyShapesRemoved",
                "castCountPreserved",
                "locationContinuityPreserved",
                "sceneStatePreserved",
            )
        },
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "assetIdentityPreserved",
        "cameraCompositionPreserved",
        "proxyShapesRemoved",
        "castCountPreserved",
        "locationContinuityPreserved",
        "sceneStatePreserved",
        "issues",
    ],
    "additionalProperties": False,
}


def mapping_field_issues(value, schema=None, path="$"):
    """Locate missing or mistyped mapping fields using the model's output contract."""
    schema = MAPPING_SCHEMA if schema is None else schema
    expected = schema["type"]
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
    }[expected]
    if not valid_type:
        return [{"path": path, "message": "Expected " + expected}]
    issues = []
    if expected == "object":
        for name in schema.get("required", []):
            if name not in value:
                issues.append({"path": path + "." + name, "message": "Required field missing"})
        for name, field in schema.get("properties", {}).items():
            if name in value:
                issues.extend(mapping_field_issues(value[name], field, path + "." + name))
    elif expected == "array":
        for index, item in enumerate(value):
            issues.extend(mapping_field_issues(item, schema["items"], f"{path}[{index}]"))
    return issues


def validate_mapping(mapping, assets, frames, scene_manifest, *, allow_ambiguous=False):
    field_issues = mapping_field_issues(mapping)
    if field_issues:
        raise GuideError("에셋 매핑 필수값 오류: " + json.dumps(field_issues, ensure_ascii=False))
    if not isinstance(mapping, dict) or not isinstance(mapping.get("cameraSummary"), str):
        raise GuideError("카메라·공간 분석 정보가 없습니다.")
    covered = []
    warnings = []
    ids = set()
    known = {
        item["id"]
        for item in (scene_manifest or {}).get("objects", [])
        if isinstance(item, dict) and "id" in item
    }
    for entity_index, entity in enumerate(mapping.get("entities", [])):
        if not entity.get("id") or entity["id"] in ids:
            raise GuideError("객체별 에셋 매핑 ID가 중복되거나 비어 있습니다.")
        ids.add(entity["id"])
        confidence = entity.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise GuideError(
                "에셋과 프리비즈 대상의 대응 관계가 불명확합니다: " + str(entity.get("label", ""))
            )
        indices = entity.get("assetIndices", [])
        if confidence < 0.65 and any(sample.get("visible") for sample in entity.get("frames", [])):
            issue = "에셋과 프리비즈 대상의 대응 관계가 불명확합니다: " + str(entity.get("label", ""))
            if not allow_ambiguous:
                raise GuideError(issue)
            warnings.append(issue)
        if not indices or any(
            type(index) is not int or not 1 <= index <= len(assets) for index in indices
        ):
            details = {
                "path": f"$.entities[{entity_index}].assetIndices",
                "entityId": entity["id"],
                "label": entity["label"],
                "receivedAssetIndices": indices,
                "allowedAssetIndices": list(range(1, len(assets) + 1)),
            }
            raise GuideError("에셋 이미지 매핑 번호가 올바르지 않습니다. "
                             + json.dumps(details, ensure_ascii=False))
        sheet_indices = [
            assets[index - 1]["sourceIndex"] for index in indices if "bbox" in assets[index - 1]
        ]
        if len(sheet_indices) != len(set(sheet_indices)):
            raise GuideError("통합 시트의 서로 다른 멤버를 하나의 인물로 매핑했습니다.")
        covered.extend(indices)
        if any(value not in known for value in entity.get("sourceObjectIds", [])):
            raise GuideError("원본에 없는 3D 객체 ID가 매핑되었습니다.")
        samples = entity.get("frames", [])
        if sorted(sample.get("index") for sample in samples) != list(range(len(frames))):
            raise GuideError("공간 가이드 프레임별 대상 매핑이 누락되었습니다.")
        for sample in samples:
            box = sample.get("bbox", [])
            if (
                len(box) != 4
                or any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                    for value in box
                )
                or type(sample.get("visible")) is not bool
            ):
                raise GuideError("공간 가이드의 2D 위치 정보가 올바르지 않습니다.")
            if sample["visible"] and (box[2] <= box[0] or box[3] <= box[1]):
                raise GuideError("보이는 대상의 화면 경계가 올바르지 않습니다.")
    if sorted(covered) != list(range(1, len(assets) + 1)):
        raise GuideError("일부 에셋이 누락되거나 중복 매핑되었습니다.")
    return warnings


class OpenAIGuideImageEditor:
    def __init__(self, api_key, *, model="gpt-image-2", transport=None, presentation_rules=""):
        self.api_key = api_key
        self.model = model
        self.transport = transport
        self.presentation_rules = presentation_rules

    async def __call__(self, body):
        if not self.api_key:
            raise GuideError("공간 가이드 이미지 생성에 OPENAI_API_KEY가 필요합니다.")
        prompt = body.get("prompt", "")
        if not prompt.startswith(MODEL_INPUT_GUARD):
            body = {**body, "prompt": MODEL_INPUT_GUARD + "\n\n" + prompt}
        body = {**body, "prompt": attach_presentation_rules(body["prompt"], self.presentation_rules)}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(240, connect=15),
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as http:
            response = await http.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": "Bearer " + self.api_key},
                json={**body, "model": self.model},
            )
        from .ai_usage import report_usage, usage_row
        from .stage_usage import STAGE_USAGE_SINK
        try:
            usage_response = response.json()
        except ValueError:
            usage_response = {}
        receipt = usage_row('openai', self.model, usage_response,
                            status='completed' if response.is_success else 'failed',
                            provider_request_id=response.headers.get('x-request-id'))
        if receipt is not None:
            report_usage(STAGE_USAGE_SINK.get(), {**receipt, 'modality': 'image'})
        if not response.is_success:
            raise GuideError(f"공간 가이드 이미지 생성 실패 (HTTP {response.status_code}).")
        try:
            content = base64.b64decode(response.json()["data"][0]["b64_json"], validate=True)
            if not 0 < len(content) <= 20 * 1024 * 1024:
                raise ValueError("image too large")
            with Image.open(BytesIO(content)) as image:
                image.verify()
            return content
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            raise GuideError("공간 가이드 이미지 결과를 확인할 수 없습니다.") from None


class SpatialGuideBuilder:
    def __init__(self, request, image_editor, *, model="gpt-6.1-terra", image_model="gpt-image-2"):
        self.request = request
        self.image_editor = image_editor
        self.model = model
        self.image_model = image_model

    async def analyze(self, name, schema, prompt, images):
        body = {
            "model": self.model,
            "instructions": prompt,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Analyze the images in their numbered order.",
                        }
                    ]
                    + [
                        {"type": "input_image", "image_url": image_data_url(path, 1024)}
                        for path in images
                    ],
                }
            ],
            "text": {
                "format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}
            },
        }
        return read_json_response(await self.request("responses", body))

    async def repair_mapping(self, bundle, prompt, images, assets, frames, scene_manifest,
                             save, check_owner):
        attempts = bundle.setdefault("mappingAttempts", [])
        candidates = []

        def consider(value):
            try:
                warnings = validate_mapping(value, assets, frames, scene_manifest, allow_ambiguous=True)
            except (GuideError, ValueError, TypeError, KeyError, AttributeError):
                return None
            candidates.append((value, warnings))
            return warnings

        for previous in attempts:
            warnings = consider(previous.get("candidate"))
            if warnings == []:
                bundle.update(mapping=previous["candidate"], mappingWarnings=[], mappingStatus="ready")
                save()
                return
        total_limit = MAPPING_REPAIR_ATTEMPTS + MAPPING_REGENERATION_ATTEMPTS
        mapping_schema = deepcopy(MAPPING_SCHEMA)
        mapping_schema["properties"]["entities"]["items"]["properties"]["assetIndices"] = {
            "type": "array",
            "minItems": 1,
            "items": {"type": "integer", "enum": list(range(1, len(assets) + 1))},
        }
        numbering_rules = (
            "assetIndices must contain the current asset index values listed in the contract. "
            "sourceIndex is provenance only: it identifies the original upload before sheet splitting. "
            "Several cropped members can share sourceIndex but have distinct current asset indices. "
            "Never use layout and timeline image numbers as assetIndices. "
            "Include only entities backed by registered assets; do not add unregistered scene objects "
            "with empty assetIndices. Offscreen registered assets keep their assetIndices and use "
            "visible=false frame samples. Assign every current asset index exactly once overall. "
        )
        contract = {
            "requiredSchema": mapping_schema,
            "assetIndices": list(range(1, len(assets) + 1)),
            "assets": [{"index": index, "role": asset["role"],
                        "label": asset.get("label", ""), "sourceIndex": asset.get("sourceIndex")}
                       for index, asset in enumerate(assets, 1)],
            "layoutIndices": list(range(len(frames))),
            "knownSourceObjectIds": [item["id"] for item in (scene_manifest or {}).get("objects", [])
                                     if isinstance(item, dict) and "id" in item],
            "hiddenEntitySample": {"index": 0, "visible": False, "bbox": [0, 0, 0, 0]},
        }
        while len(attempts) < total_limit:
            # Ambiguous but structurally usable mappings already have a warning fallback.
            if len(attempts) >= MAPPING_REPAIR_ATTEMPTS and candidates:
                break
            regenerating = len(attempts) >= MAPPING_REPAIR_ATTEMPTS
            if check_owner:
                check_owner()
            feedback = ""
            if attempts:
                feedback = (
                    "\nREPAIR THE MAPPING. Correct the reported issue and return the complete mapping. "
                    "Preserve valid assignments and camera framing. Confidence reflects assignment consistency. "
                    "For background references, determine their intended environment regions and appearance roles "
                    "from the brief and assets. Several background assets may form complementary regions of one "
                    "environment. Use layout frames for viewpoint and placement; proxy texture resemblance is not "
                    "an identity requirement. Keep genuinely uncertain assignments marked uncertain. "
                    + json.dumps({"validationIssue": attempts[-1].get("issue"),
                                  "previousCandidate": attempts[-1].get("candidate")}, ensure_ascii=False)
                )
            if regenerating:
                feedback = (
                    "\nREGENERATE THE COMPLETE MAPPING FROM THE ORIGINAL IMAGES. "
                    "Previous repair responses lack usable required data. Re-evaluate the current assets and layouts "
                    "and return a fresh complete mapping; retain only identities supported by the current inputs. "
                    "Hidden or not-yet-introduced characters still require one visible=false, zero-bbox sample "
                    "per layout. Do not force an offscreen character into the shot or invent positions. "
                    + json.dumps({"validationIssues": [attempt.get("issue") for attempt in attempts],
                                  "fieldIssues": attempts[-1].get("fieldIssues", [])}, ensure_ascii=False)
                )
            feedback += "\n" + numbering_rules
            feedback += "\nMANDATORY MAPPING INPUT/OUTPUT CONTRACT\n" + json.dumps(contract, ensure_ascii=False)
            record = {"attempt": len(attempts) + 1, "candidate": None,
                      "mode": "regenerate" if regenerating else "repair"}
            try:
                record["candidate"] = await self.analyze(
                    "spatial_asset_mapping", mapping_schema, prompt + feedback, images,
                )
                record["fieldIssues"] = mapping_field_issues(record["candidate"])
                validate_mapping(record["candidate"], assets, frames, scene_manifest)
                record["issue"] = None
            except (GuideError, ValueError, TypeError, KeyError, AttributeError, httpx.HTTPError) as error:
                record["issue"] = type(error).__name__ if isinstance(error, httpx.HTTPError) else str(error)
            if check_owner:
                check_owner()
            attempts.append(record)
            consider(record["candidate"])
            bundle["mappingStatus"] = "repairing" if record["issue"] else "ready"
            save()
            if not record["issue"]:
                bundle.update(mapping=record["candidate"], mappingWarnings=[])
                save()
                return
            logging.getLogger("uvicorn.error").info(
                "[asset-mapping] 검증 실패 %d/%d · %s · AI %s 대상",
                record["attempt"], total_limit, record["issue"],
                "재생성" if regenerating else "보정",
            )
        if candidates:
            mapping, warnings = min(candidates, key=lambda item: (
                len(item[1]), -min(entity["confidence"] for entity in item[0]["entities"]),
            ))
            bundle.update(mapping=mapping, mappingWarnings=warnings, mappingStatus="ready_with_warnings")
            save()
            logging.getLogger("uvicorn.error").info(
                "[asset-mapping] 보정 3회 완료 · 보정 필요 %d건 · 이미지 생성 계속", len(warnings),
            )
            return
        bundle["mappingStatus"] = "unresolved"
        save()
        last_issue = attempts[-1].get("issue", "필수 매핑 데이터 누락") if attempts else "필수 매핑 데이터 누락"
        raise GuideError(
            f"에셋 매핑 보정·재생성 {total_limit}회 후에도 필수값을 완성하지 못했습니다. "
            f"마지막 원인: {last_issue}. 저장된 mappingAttempts를 확인하세요."
        )

    async def detect_asset_parts(self, assets):
        characters = [
            (index, asset) for index, asset in enumerate(assets, 1) if asset["role"] == "character"
        ]
        analysis = {"sheets": []}
        if characters:
            analysis = await self.analyze(
                "spatial_character_sheet",
                CAST_SHEET_SCHEMA,
                "Inspect these character references for structured sheets of DIFFERENT named or visually distinct people. "
                "Return one sheets entry per input, using these original imageIndex values in order: "
                + json.dumps([index for index, _ in characters])
                + ". "
                "For a multi-person sheet locate one non-overlapping rectangular panel per distinct person. "
                "Include their face and full-body turnaround views together in their panel. Use the label printed "
                "for that person when readable. Front, side, back and detail views of one person are a single identity. "
                "For a single-person sheet or a group photo without separable panels return an empty members list. "
                "bbox is [left, top, right, bottom] relative to the full input image in the range 0 to 1.",
                [Path(asset["path"]) for _, asset in characters],
            )
        return character_asset_parts(assets, analysis)

    async def select_key_scenes(
        self, source, directory, *, limit, brief="", scene_manifest=None, on_sampled=None
    ):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise GuideError("주요 씬 가이드 한도는 1~64장입니다.")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        candidates = await asyncio.to_thread(scene_candidates, source, directory / "candidates")
        if on_sampled:
            on_sampled()
        descriptions = [
            {key: value for key, value in frame.items() if key != "path"} for frame in candidates
        ]
        native = []
        for candidate in candidates:
            values = (scene_manifest or {}).get("frames", [])
            if values:
                nearest = min(
                    values, key=lambda value: abs(value.get("time", 0) - candidate["time"])
                )
                native.append(
                    {
                        "candidateIndex": candidate["index"],
                        "camera": nearest.get("camera"),
                        "subjects": nearest.get("subjects", []),
                    }
                )
        prompt = (
            "Select the major SPATIAL guide scenes from the ENTIRE chronological previs survey, not equally spaced instants. "
            "Use exactly these criteria: scene_transition = a new setting/background/location; "
            "camera_change = substantial viewpoint or shot-size change such as front to side or wide to close; "
            "character_appearance = first entry or reappearance after occlusion/offscreen absence; "
            "staging_change = major change in relative character/object placement; final_framing = ending composition. "
            "Select the clearest frame at/just after each change so the new scene/cast is visible. "
            "Do not select minor joint pose changes or near-duplicate views. One frame may cover several criteria. "
            f"The image budget is {limit}; opening and final frames will be reserved when budget permits. "
            "Rank additional scenes by importance and coverage of distinct cast, settings, viewpoints and reappearances. "
            "Do not fill unused slots with duplicates. If important events exceed capacity, choose representative scenes and explain omitted coverage in coverageNotes. "
            "Return only valid candidateIndex values, one entry per candidate, with reasons and short labels. "
            "Frame images are ordered exactly as candidate indices. Data: "
            + json.dumps(
                {"brief": brief, "candidates": descriptions, "native": native}, ensure_ascii=False
            )
        )
        selected = await self.analyze(
            "spatial_scene_selection",
            SCENE_SELECTION_SCHEMA,
            prompt,
            [frame["path"] for frame in candidates],
        )
        scenes = selected.get("scenes")
        seen = set()
        if not isinstance(scenes, list) or not isinstance(selected.get("coverageNotes"), str):
            raise GuideError("주요 씬 선택 응답이 올바르지 않습니다.")
        for scene in scenes:
            idx = scene.get("candidateIndex")
            score = scene.get("importance")
            reasons = scene.get("reasons")
            if (
                type(idx) is not int
                or not 0 <= idx < len(candidates)
                or idx in seen
                or type(score) not in (int, float)
                or not math.isfinite(score)
                or not 0 <= score <= 1
                or not isinstance(scene.get("label"), str)
                or not isinstance(reasons, list)
                or not reasons
                or any(reason not in SCENE_CRITERIA for reason in reasons)
            ):
                raise GuideError("주요 씬 선택의 프레임 번호·선정 사유가 올바르지 않습니다.")
            seen.add(idx)
        by_index = {scene["candidateIndex"]: scene for scene in scenes}
        final = len(candidates) - 1
        reserved = [final] if limit == 1 else list(dict.fromkeys([0, final]))
        for idx in reserved:
            if idx not in by_index:
                by_index[idx] = {
                    "candidateIndex": idx,
                    "reasons": ["final_framing" if idx == final else "camera_change"],
                    "label": "최종 구도" if idx == final else "시작 구도",
                    "importance": 1,
                }
        if "final_framing" not in by_index[final]["reasons"]:
            by_index[final]["reasons"].append("final_framing")
        additional = sorted(
            (scene for idx, scene in by_index.items() if idx not in reserved),
            key=lambda scene: (-scene["importance"], scene["candidateIndex"]),
        )
        indices = sorted(
            reserved
            + [scene["candidateIndex"] for scene in additional[: max(0, limit - len(reserved))]]
        )
        frames = await asyncio.to_thread(
            sample_frames,
            source,
            directory,
            len(indices),
            "references",
            frame_indices=[candidates[idx]["frame"] for idx in indices],
        )
        for frame, idx in zip(frames, indices):
            frame.update(
                {key: by_index[idx][key] for key in ("reasons", "label", "importance")},
                candidateIndex=idx,
            )
        report = {
            "policy": "key_scenes_v1",
            "budget": limit,
            "candidateCount": len(candidates),
            "coverageNotes": selected["coverageNotes"],
            "omittedCandidateCount": len(set(by_index) - set(indices)),
            "scenes": [
                {key: value for key, value in frame.items() if key != "path"} for frame in frames
            ],
        }
        return {"frames": frames, "report": report}

    async def build(
        self,
        source,
        assets,
        directory,
        *,
        count=3,
        brief="",
        input_format="references",
        scene_manifest=None,
        progress=None,
        check_owner=None,
        selection="uniform",
        scenario_frames=None,
    ):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise GuideError("공간 가이드 저장 경로가 올바르지 않습니다.")
        if not assets:
            raise GuideError("공간 가이드를 만들 캐릭터·배경 에셋을 등록하세요.")
        signature = {
            "version": GUIDE_VERSION,
            "model": self.model,
            "imageModel": self.image_model,
            "brief": brief,
            "count": count,
            "inputFormat": input_format,
            "source": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            "assets": [
                (
                    asset["id"],
                    asset["role"],
                    hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest(),
                )
                for asset in assets
            ],
            "sceneManifest": scene_manifest,
        }
        if selection != "uniform":
            signature["selection"] = selection
        if scenario_frames:
            signature["scenarioFrames"] = scenario_frames
        fingerprint = hashlib.sha256(
            json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        manifest_path = directory / "manifest.json"
        bundle = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        )
        if bundle and bundle.get("fingerprint") != fingerprint:
            raise GuideError(
                "저장된 공간 가이드 입력과 현재 요청이 다릅니다. 새 요청 ID를 사용하세요."
            )
        bundle = bundle or {
            "version": GUIDE_VERSION,
            "fingerprint": fingerprint,
            "sourceType": "blender_native" if scene_manifest else "video_2d_analysis",
            "status": "mapping",
            "guides": [],
        }

        def save():
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            temporary.replace(manifest_path)
            if progress:
                progress(bundle)

        if scenario_frames and input_format == "references":
            frames = await asyncio.to_thread(
                sample_frames, source, directory, len(scenario_frames), input_format,
                frame_fractions=[scene["fraction"] for scene in scenario_frames],
            )
            for frame, scene in zip(frames, scenario_frames):
                frame.update(label=scene["title"], scenarioSceneIndex=scene["index"])
        elif selection == "key_scenes_v1" and input_format == "references":
            if "selection" not in bundle:
                chosen = await self.select_key_scenes(
                    source,
                    directory,
                    limit=count,
                    brief=brief,
                    scene_manifest=scene_manifest,
                    on_sampled=save,
                )
                bundle["selection"] = chosen["report"]
                save()
                frames = chosen["frames"]
            else:
                chosen = bundle["selection"]["scenes"]
                frames = await asyncio.to_thread(
                    sample_frames,
                    source,
                    directory,
                    len(chosen),
                    "references",
                    frame_indices=[item["frame"] for item in chosen],
                )
                for frame, item in zip(frames, chosen):
                    frame.update(
                        {
                            key: item[key]
                            for key in ("reasons", "label", "importance", "candidateIndex")
                        }
                    )
        else:
            frames = await asyncio.to_thread(sample_frames, source, directory, count, input_format)
        timeline_directory = directory / "timeline"
        timeline_directory.mkdir(exist_ok=True)
        timeline = (
            []
            if input_format == "first_frame"
            else await asyncio.to_thread(sample_frames, source, timeline_directory, 3, "references")
        )
        save()  # Retain/register sampled files even if subsequent mapping fails.
        original_assets = assets
        if "assetParts" not in bundle and "mapping" not in bundle:
            bundle["assetParts"] = await self.detect_asset_parts(assets)
            save()
        if "assetParts" in bundle:
            assets = await asyncio.to_thread(
                prepare_asset_parts, assets, bundle["assetParts"], directory
            )
            save()
        if "mapping" in bundle:
            try:
                validate_mapping(bundle["mapping"], assets, frames, scene_manifest, allow_ambiguous=True)
            except (GuideError, ValueError, TypeError, KeyError, AttributeError):
                bundle.pop("mapping")
                bundle["mappingStatus"] = "repairing"
                save()
        if "mapping" not in bundle:
            native = {"objects": (scene_manifest or {}).get("objects", []), "frames": []}
            for frame in frames:
                candidates = (scene_manifest or {}).get("frames", [])
                if candidates:
                    native["frames"].append(
                        min(candidates, key=lambda value: abs(value.get("time", 0) - frame["time"]))
                    )
            prompt = (
                "Map asset identities to the same entities across every layout frame. The first "
                + str(len(assets))
                + " images are ASSETS in listed order; the next "
                + str(len(frames))
                + " are SELECTED layouts to map. The final "
                + str(len(timeline))
                + " images are TIMELINE-ONLY context, not additional mapping samples. "
                "Use assets for final identity, proportions, clothing/materials; layouts only for position/camera/visibility. "
                "Use a separate entity for every labeled member panel of the same original sheet. "
                "Group separate references of the same person under one ID. Include every input asset once. "
                "Anchor assignments in a clear wide frame. If identical proxy models lack identity cues, assign the sheet "
                "members in panel order to distinct people in that anchor frame, then follow motion continuity or stable "
                "native object IDs through every frame. Keep IDs during occlusion and do not reassign by screen order "
                "after the camera moves. Confidence measures the consistency of the assignment, not the face resemblance "
                "of a stylized proxy to the asset. "
                "For background assets, map their intended final environment regions using the brief and asset "
                "roles. Multiple background references can describe complementary regions of one environment. "
                "A plain proxy sea or terrain need not resemble the final background texture. "
                "For each entity provide one sample per selected layout index ONLY; do not create mapping samples for timeline-only images. bbox=[left,top,right,bottom] normalized 0..1; hidden entities have visible=false and bbox=[0,0,0,0]. "
                "Only assign sourceObjectIds present in native metadata; leave empty for uploaded MP4. Do not invent 3D positions. "
                "Give low confidence for unresolved visible identities; an offscreen asset need not be matched to a visible proxy. Camera summary must state viewpoint/framing, travel direction and end framing, not mannequin poses. "
                "Input data: "
                + json.dumps(
                    {
                        "brief": brief,
                        "assets": [
                            {
                                "index": i + 1,
                                "role": asset["role"],
                                **{
                                    key: asset[key]
                                    for key in ("sourceIndex", "label", "bbox")
                                    if key in asset
                                },
                            }
                            for i, asset in enumerate(assets)
                        ],
                        "layouts": [
                            {
                                "index": frame["index"],
                                "time": frame["time"],
                                "imageNumber": len(assets) + i + 1,
                            }
                            for i, frame in enumerate(frames)
                        ],
                        "timelineOnly": [
                            {
                                "time": frame["time"],
                                "imageNumber": len(assets) + len(frames) + i + 1,
                            }
                            for i, frame in enumerate(timeline)
                        ],
                        "native": native,
                    },
                    ensure_ascii=False,
                )
            )
            await self.repair_mapping(
                bundle, prompt,
                [Path(asset["path"]) for asset in assets]
                + [frame["path"] for frame in frames]
                + [frame["path"] for frame in timeline],
                assets, frames, scene_manifest, save, check_owner,
            )
        validate_mapping(bundle["mapping"], assets, frames, scene_manifest,
                         allow_ambiguous=bool(bundle.get("mappingWarnings")))
        for frame in frames:
            index = frame["index"]
            entry = next((item for item in bundle["guides"] if item["index"] == index), None)
            if entry is None:
                entry = {
                    key: frame[key]
                    for key in ("index", "frame", "time", "width", "height", "reasons", "label", "scenarioSceneIndex")
                    if key in frame
                }
                entry.update(name=f"spatial-{index}.png", status="pending")
                bundle["guides"].append(entry)
            target = directory / entry["name"]
            previous = next((item for item in bundle["guides"]
                             if item["index"] == index - 1 and item.get("status") == "verified"), None)
            previous_path = directory / previous["name"] if previous else None
            if previous_path is not None and (
                not previous_path.is_file()
                or hashlib.sha256(previous_path.read_bytes()).hexdigest() != previous.get("sha256")
            ):
                raise GuideError("직전 씬의 공간 가이드 이미지가 변경되거나 누락되었습니다.")
            while entry["status"] not in ("excluded", "unknown", "requesting"):
                if (
                    entry["status"] == "invalid"
                    and entry.get("generationAttempts", 1) >= GUIDE_GENERATION_ATTEMPTS
                ):
                    entry["status"] = "excluded"
                    save()
                    break
                if entry["status"] == "verified" and (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != entry.get("sha256")
                ):
                    raise GuideError("저장된 공간 가이드 이미지가 변경되거나 누락되었습니다.")
                if entry["status"] not in ("generated", "verified"):
                    if check_owner:
                        check_owner()
                    entry["generationAttempts"] = entry.get("generationAttempts", 0) + 1
                    entry["status"] = "requesting"
                    save()
                    width, height = frame["width"], frame["height"]
                    if not 1 / 3 <= width / height <= 3:
                        raise GuideError("공간 가이드 이미지 비율은 1:3~3:1이어야 합니다.")
                    scale = 1536 / max(width, height)
                    size = (
                        max(16, round(width * scale / 16) * 16),
                        max(16, round(height * scale / 16) * 16),
                    )
                    entities = []
                    visible_indices = set()
                    hidden = []
                    for entity in bundle["mapping"]["entities"]:
                        sample = next(
                            sample for sample in entity["frames"] if sample["index"] == index
                        )
                        if not sample["visible"]:
                            hidden.append(entity["label"])
                            continue
                        visible_indices.update(entity["assetIndices"])
                        entities.append(
                            {
                                **{
                                    key: entity[key]
                                    for key in ("id", "label", "appearance", "assetIndices")
                                },
                                "bbox": sample["bbox"],
                            }
                        )
                    visible_assets = [
                        (i, asset) for i, asset in enumerate(assets, 1) if i in visible_indices
                    ]
                    asset_limit = 13 if previous_path else 14
                    if len(visible_assets) > asset_limit:
                        # Reserve layout, preceding scene, and rejected candidate on repair.
                        parent_indices = sorted(
                            {asset["sourceIndex"] for _, asset in visible_assets}
                        )
                        visible_assets = [
                            (number, original_assets[number - 1]) for number in parent_indices
                        ]
                        if len(visible_assets) > asset_limit:
                            raise GuideError("공간 가이드의 에셋 수가 이미지 참조 한도를 초과합니다.")
                        edit_indices = {
                            index: parent_indices.index(assets[index - 1]["sourceIndex"]) + 2
                            for index in visible_indices
                        }
                        for entity in entities:
                            entity["sheetPanels"] = [
                                {
                                    "image": edit_indices[index],
                                    "label": assets[index - 1]["label"],
                                    "bbox": assets[index - 1]["bbox"],
                                }
                                for index in entity["assetIndices"]
                                if "bbox" in assets[index - 1]
                            ]
                    else:
                        edit_indices = {
                            original: position + 2
                            for position, (original, _) in enumerate(visible_assets)
                        }
                    for entity in entities:
                        entity["editImageNumbers"] = sorted(
                            {edit_indices[value] for value in entity.pop("assetIndices")}
                        )
                    prompt = (
                        "Edit Image 1 as an exact camera-layout reconstruction at this SINGLE instant. "
                        "Image 1 is the authoritative layout: preserve its viewpoint, subject center coordinates, bounding-box scale, orientation, supports, contact and occlusion. "
                        "Original asset images supply appearance only, NEVER their subject scale, position or pose. "
                        "Rebuild visible entities with the mapped asset anatomy/materials; remove wooden/proxy shapes and make a coherent environment. "
                        "Keep a perched animal perched if perched in Image 1. Do not execute later actions or invent a more dramatic composition. "
                        "Use the scene brief for the location and action at this timestamp; mapped appearance fields define the visible entities. "
                        "Maintain connected indoor/outdoor geography and persistent doorway, wall and prop structures across endpoints. "
                        "An exterior appearance reference can establish the adjacent building while the current layout remains indoors. "
                        "The normalized bbox for each entity is a spatial constraint: retain its screen center and approximate extent with natural asset proportions. "
                        "Only listed visible entities may appear. Excluded/offscreen entities MUST remain absent, even when in the overall story. "
                        "No panels, labels, text annotations or reference sheets. "
                        "Snapshot constraints: "
                        + json.dumps(
                            {
                                "time": frame["time"],
                                "sceneBrief": brief,
                                "cameraSummary": bundle["mapping"].get("cameraSummary", ""),
                                "visibleEntities": entities,
                                "excludedEntities": hidden,
                            },
                            ensure_ascii=False,
                        )
                    )
                    body = {
                        "images": [{"image_url": image_data_url(frame["path"])}]
                        + [
                            {"image_url": image_data_url(asset["path"])}
                            for original, asset in visible_assets
                        ],
                        "prompt": prompt,
                        "quality": "medium",
                        "size": f"{size[0]}x{size[1]}",
                        "output_format": "png",
                        "n": 1,
                        "background": "opaque",
                    }
                    if previous_path:
                        body["images"].append({"image_url": image_data_url(previous_path)})
                        body["prompt"] += (
                            f" Image {len(body['images'])} is the PREVIOUS SCENE continuity reference. "
                            "Inherit its actual environment architecture, landmarks, materials, lighting, wardrobe, "
                            "prop identity and physical state. Evolve character positions, facing, contacts and camera "
                            "naturally toward the CURRENT Image 1 layout and action phase; do not freeze or copy "
                            "the previous pose or reset the environment. Original assets remain authoritative for identity. "
                            "Allow explicit location changes, cuts and time jumps stated in the scene brief; "
                            "otherwise preserve the same connected space and persistent objects."
                        )
                        entry["continuityReference"] = {"index": previous["index"], "sha256": previous["sha256"]}
                    if entry.get("verification"):
                        body["prompt"] += (
                            " REPAIR INSTRUCTIONS: Compare the original layout (Image 1) with the rejected last image. "
                            "Fix each verification issue using the original camera angle, depth order, relative sizes, "
                            "bounding boxes and occlusion. For an oblique lineup, keep its diagonal recession and "
                            "overlap where shown. Preserve the asset identity and keep the original layout as the target. "
                            "Verification issues: "
                            + json.dumps(entry["verification"]["issues"], ensure_ascii=False)
                        )
                        body["images"].append({"image_url": image_data_url(target)})
                    try:
                        content = await self.image_editor(body)
                        with Image.open(BytesIO(content)) as image:
                            image.verify()
                    except (httpx.HTTPError, GuideError, OSError, ValueError) as error:
                        entry.update(status="unknown", generationIssue=type(error).__name__)
                        save()
                        break
                    if check_owner:
                        check_owner()
                    temporary = target.with_suffix(".part")
                    temporary.write_bytes(content)
                    temporary.replace(target)
                    with Image.open(target) as image:
                        entry.update(width=image.width, height=image.height)
                    entry.update(status="generated", sha256=hashlib.sha256(content).hexdigest())
                    save()
                if entry["status"] != "verified":
                    try:
                        verification = await self.analyze(
                            "spatial_guide_verification",
                            VERIFY_SCHEMA,
                            "Verify the edited guide: first image=rough layout, next "
                            + str(len(assets))
                            + "=assets, then an optional previous-scene reference, final image=generated spatial guide. "
                            "Check only entities expected visible in this frame; preserve hidden/offscreen state. Judge asset identity/proportions/materials, camera composition/positions, removal of proxy geometry, and intended cast count. "
                            "A textured proxy or extra mannequin is a failure. Do not require an off-screen entity to appear. For each issue specify the correction needed from the original layout, including perspective and overlap when relevant. "
                            "Check locationContinuityPreserved against the scene brief and source layout: retain the indoor/outdoor "
                            "location and connected doorways at this timestamp. Use the previous-scene reference, when present, "
                            "to check architecture, landmarks, lighting and persistent prop continuity. Allow explicit location changes "
                            "and time jumps in the brief. Do not require identical screen positions or poses: use the current layout "
                            "and action phase to judge natural state progression. Check sceneStatePreserved for the exact action phase, "
                            "cast positions, carrying/supporting contacts and prop/lid state; a carrier standing outside a bin remains outside. "
                            "Identify the specific image correction for a disagreement. Keep the source action sequence intact. "
                            "Mapping and frame: "
                            + json.dumps(
                                {"mapping": bundle["mapping"], "index": index, "time": frame["time"],
                                 "sceneBrief": brief, "previousSceneIncluded": previous_path is not None,
                                 "previousSceneIndex": previous["index"] if previous else None},
                                ensure_ascii=False,
                            ),
                            [frame["path"]] + [Path(asset["path"]) for asset in assets]
                            + ([previous_path] if previous_path else [])
                            + [target],
                        )
                        if not isinstance(verification, dict) or not isinstance(
                            verification.get("issues"), list
                        ):
                            raise GuideError("공간 가이드 검증 응답 형식이 올바르지 않습니다.")
                    except (httpx.HTTPError, RuntimeError) as error:
                        entry.update(status="excluded", verificationIssue=type(error).__name__)
                        save()
                        break
                    valid = all(
                        verification.get(key) is True
                        for key in (
                            "assetIdentityPreserved",
                            "cameraCompositionPreserved",
                            "proxyShapesRemoved",
                            "castCountPreserved",
                            "locationContinuityPreserved",
                            "sceneStatePreserved",
                        )
                    )
                    entry.update(
                        status="verified" if valid else "invalid", verification=verification
                    )
                    save()
                    if not valid:
                        continue
                break
        bundle["status"] = (
            "ready"
            if all(guide["status"] == "verified" for guide in bundle["guides"])
            else "partial"
        )
        save()
        return bundle

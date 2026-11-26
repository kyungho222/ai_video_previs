"""Asset-based still guides, independent of optional motion/video adapters."""

import asyncio
import base64
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import logging
import math
import re
from .contract_validation import contract_issues
from pathlib import Path
import httpx
from PIL import Image, ImageOps
from .video_errors import VideoError
from .video_prompt import PREVIS_RULES
from .previs_reference import scene_brief_for_frame
from .scene_flow import FIRST_SCENE_CONTINUITY, SCENE_FLOW_RULES
from .prompt_filter import MODEL_INPUT_GUARD, attach_presentation_rules
from .sheet_layout import detect_sheet_layout
from .character_sheet_adapter import compare_sheet_layout
from .analysis_sheet import create_analysis_sheets
from .asset_mapping_inventory import build_asset_inventory, assess_asset_inventory, complement_mapping_from_native

GUIDE_VERSION = "spatial-guides-v3-coordinate-contract"
GUIDE_GENERATION_ATTEMPTS = 3
ENDPOINT_GENERATION_ATTEMPTS = 2
MAPPING_REPAIR_ATTEMPTS = 2
MAPPING_REGENERATION_ATTEMPTS = 0

VIDEO_ONLY_MAPPING_RULES = (
    "This is video-frame-only asset mapping. No original 3D object metadata is available in this task. "
    "Match the supplied assets to observed 2D regions in the video frames. "
    "For EVERY entity return sourceObjectIds=[] without exception. Do not invent, infer or request "
    "original 3D object IDs, meshes or transforms. Names in scene descriptions are not 3D metadata. "
    "Create stable entity id values only for the 2D mapping and preserve each identity across frames, "
    "including separately cropped members of a combined character sheet. "
)


class GuideError(VideoError):
    pass


def _response_diagnostic_error(message, code, response, *, json_error=None):
    error = GuideError(message)
    status = response.get('status') if isinstance(response, dict) else None
    output = response.get('output') if isinstance(response, dict) else None
    output = output if isinstance(output, list) else []
    parts = [part for item in output if isinstance(item, dict)
             for part in (item.get('content') if isinstance(item.get('content'), list) else [])
             if isinstance(part, dict) and part.get('type') == 'output_text']
    error.guide_diagnostic = {'code': code,
        'responseStatus': status if status in ('completed', 'incomplete', 'failed', 'in_progress', 'queued') else None,
        'outputItems': len(output), 'textParts': len(parts)}
    if isinstance(json_error, json.JSONDecodeError):
        error.guide_diagnostic['jsonErrorPosition'] = json_error.pos
    return error


def guide_verification_diagnostic(error, *, model, index):
    result = {'stage': 'guide_verification', 'index': index, 'model': model,
              'errorType': type(error).__name__, 'code': 'verification_error'}
    details = getattr(error, 'guide_diagnostic', {})
    for key in ('code', 'responseStatus', 'outputItems', 'textParts', 'jsonErrorPosition', 'missingFields', 'invalidFields', 'responseShape', 'structuralKeys', 'wrapperKeys', 'checkValues'):
        if key in details:
            result[key] = details[key]
    if isinstance(error, httpx.HTTPStatusError):
        result['httpStatus'] = error.response.status_code
    return result


def normalize_model_mapping(value):
    from .mapping_coordinates import normalize_model_mapping as convert
    return convert(value,error_type=GuideError)


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
    lines = ["SPATIAL IMAGE ROLES", "Keep the mapped identities through every shot; no extra cast."]
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
        raise _response_diagnostic_error("공간 가이드 분석 응답이 완료되지 않았습니다.", 'response_incomplete', response)
    try:
        text = "".join(
            part.get("text", "")
            for item in response.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        return json.loads(text)
    except (ValueError, AttributeError, TypeError) as error:
        raise _response_diagnostic_error("공간 가이드 분석 응답을 해석할 수 없습니다.", 'invalid_json', response, json_error=error) from None


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
                                "bbox": {"type": "array", "minItems": 4, "maxItems": 4,
                                         "items": {"type": "number", "minimum": 0, "maximum": 1}},
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


def normalize_character_sheet_indices(analysis, characters):
    """Translate complete, unambiguous image sequences to original asset indices."""
    result = deepcopy(analysis)
    sheets = result.get('sheets') if isinstance(result, dict) else None
    if not isinstance(sheets, list) or len(sheets) != len(characters):
        return result
    indices = [row.get('imageIndex') if isinstance(row, dict) else None for row in sheets]
    if any(type(index) is not int for index in indices) or len(set(indices)) != len(indices):
        return result
    originals = [index for index, _ in characters]
    if set(indices) == set(originals):
        return result
    if set(indices) == set(range(len(characters))):
        mapping = dict(enumerate(originals))
    elif set(indices) == set(range(1, len(characters) + 1)):
        mapping = {i + 1: value for i, value in enumerate(originals)}
    else:
        return result
    for sheet in sheets:
        sheet['imageIndex'] = mapping[sheet['imageIndex']]
    return result


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
        for member_index, member in enumerate(members, 1):
            if not isinstance(member, dict):
                raise GuideError(f"통합 시트 {index}번의 멤버 {member_index}: 객체 형식이 필요합니다.")
            box, label = member.get("bbox"), member.get("label")
            if not isinstance(label, str) or not label.strip() or label.strip() in labels:
                raise GuideError(f"통합 시트 {index}번의 멤버 {member_index}: label이 비었거나 중복됐습니다.")
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in box)
                or box[2] - box[0] < 0.02
                or box[3] - box[1] < 0.02
            ):
                raise GuideError(
                    f"통합 시트 {index}번의 멤버 {member_index}: bbox는 0..1 범위의 "
                    "[left,top,right,bottom] 좌표 4개이며 너비·높이는 각각 0.02 이상이어야 합니다."
                )
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
                    "appearance": {"type": "string", "description": "Final appearance from the asset references only; never describe layout proxy geometry as the asset appearance."},
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


async def verify_guide_response(analyze, prompt, images, *, model, index):
    """Inspect the single response; never infer a missing check passed."""
    attempts = []
    for attempt in (1,):
        value = await analyze('spatial_guide_verification', VERIFY_SCHEMA, prompt, images)
        required = VERIFY_SCHEMA['required']
        path = []
        wrappers = ('result', 'verification', 'data', 'analysis', 'output')
        structural_keys = ('type', 'properties', 'required', 'additionalProperties', 'schema', '$schema',
                           'checks', 'results', 'assessment', 'summary', 'reason', 'status')
        present_structure = [key for key in structural_keys if isinstance(value, dict) and key in value]
        response_shape = ('schema_definition' if isinstance(value, dict) and value.get('type') == 'object'
                          and isinstance(value.get('properties'), dict) else 'other')
        wrapper_keys = [key for key in wrappers if isinstance(value, dict) and key in value]
        # A wrapper is unambiguous only when it contains one fully-typed result
        # and the outer object does not itself provide partial verification data.
        if isinstance(value, dict) and not (set(value) & set(required)):
            candidates = []
            def visit(node, parent):
                if not isinstance(node, dict) or len(parent) > 2:
                    return
                if all(key in node for key in required) and all(type(node[key]) is bool for key in required if key != 'issues') and isinstance(node['issues'], list) and all(isinstance(v, str) for v in node['issues']):
                    candidates.append((parent, node))
                    return
                if set(node) & set(required):
                    return
                for key in wrappers:
                    if key in node:
                        visit(node[key], parent + [key])
            visit(value, [])
            if len(candidates) == 1:
                path, value = candidates[0]
        missing = [key for key in required if not isinstance(value, dict) or key not in value]
        schema_issues = contract_issues(value, VERIFY_SCHEMA, allow_extra=True)
        invalid = [key for key in required if key not in missing and any(
            issue['path'] == key or issue['path'].startswith(key + '[') for issue in schema_issues)]
        attempt_info = {'attempt': attempt, 'missingFields': missing, 'invalidFields': invalid,
                        'contractIssues': schema_issues,
                        'wrapperKeys': wrapper_keys,
                        'responseShape': response_shape, 'structuralKeys': present_structure,
                        'checkValues': {key: value[key] for key in required if key != 'issues'
                                        and isinstance(value, dict) and type(value.get(key)) is bool},
                        'extraFieldCount': len(set(value) - set(required)) if isinstance(value, dict) else 0}
        if path:
            attempt_info['normalizationPath'] = path
        attempts.append(attempt_info)
        if not schema_issues:
            return value, attempts
        logging.getLogger('uvicorn.error').warning(
            '[spatial-guide-verification] 검증 응답 형식 오류 · %s',
            json.dumps({'index': index, 'model': model, **attempt_info}, ensure_ascii=False))
    error = GuideError('공간 가이드 검증 응답 형식이 올바르지 않습니다.')
    error.guide_diagnostic = {'code': 'invalid_verification_format',
                              'missingFields': attempts[-1]['missingFields'],
                              'invalidFields': attempts[-1]['invalidFields'],
                              'responseShape': attempts[-1]['responseShape'],
                              'structuralKeys': attempts[-1]['structuralKeys'],
                              'wrapperKeys': attempts[-1]['wrapperKeys'],
                              'checkValues': attempts[-1]['checkValues']}
    error.verification_attempts = attempts
    raise error


def mapping_field_issues(value, schema=None, path="$"):
    """Locate missing or mistyped mapping fields using the model's output contract."""
    schema = MAPPING_SCHEMA if schema is None else schema
    issues = contract_issues(value, schema, path, allow_extra=True)
    for issue in issues:
        if issue['message'].startswith('expected '):
            issue['message'] = 'Expected ' + issue['message'][9:]
        elif issue['message'] == 'required field missing':
            issue['message'] = 'Required field missing'
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
        unknown = [value for value in entity.get("sourceObjectIds", []) if value not in known]
        if unknown:
            raise GuideError("원본에 없는 3D 객체 ID가 매핑되었습니다: " + json.dumps({
                'entityId': entity['id'], 'unknownSourceObjectIds': unknown,
                'knownSourceObjectIds': sorted(known),
            }, ensure_ascii=False))
        samples = entity.get("frames", [])
        received = [sample["index"] for sample in samples]
        expected = list(range(len(frames)))
        if sorted(received) != expected:
            details = {
                "path": f"$.entities[{entity_index}].frames",
                "entityId": entity["id"],
                "expectedIndices": expected,
                "receivedIndices": received,
                "missingIndices": sorted(set(expected) - set(received)),
                "unexpectedIndices": sorted(set(received) - set(expected)),
                "duplicateIndices": sorted({i for i in received if received.count(i) > 1}),
            }
            raise GuideError("공간 가이드 프레임별 대상 매핑이 누락되거나 중복되었습니다. "
                             + json.dumps(details, ensure_ascii=False))
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
        missing = sorted(set(range(1, len(assets) + 1)) - set(covered))
        details = {
            "missingAssets": [{"index": i, "role": assets[i - 1].get("role"),
                               "label": assets[i - 1].get("label", ""),
                               "name": assets[i - 1].get("name") or Path(str(assets[i - 1].get("path", ""))).name,
                               "sourceIndex": assets[i - 1].get("sourceIndex")} for i in missing],
            "duplicateAssetIndices": sorted({i for i in covered if covered.count(i) > 1}),
            "receivedAssetIndices": covered,
        }
        raise GuideError("일부 에셋이 누락되거나 중복 매핑되었습니다. "
                         + json.dumps(details, ensure_ascii=False))
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
                        {"type": "input_image", "image_url": image_data_url(path, 768)}
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
                             save, check_owner, *, mapping_request=None, analysis_sheets=None, sheet_refs=None):
        attempts = bundle.setdefault("mappingAttempts", [])
        inventory = build_asset_inventory(assets)
        bundle['assetMappingInventory'] = assess_asset_inventory(inventory, {}, frames, scene_manifest)
        save()
        candidates = []
        known_source_ids = sorted({item['id'] for item in (scene_manifest or {}).get('objects', [])
                                   if isinstance(item, dict) and isinstance(item.get('id'), str)})

        def apply_source_id_policy(value):
            # With no exported 3D metadata there is no source-ID namespace to map.
            # Entity identities, asset assignments and observed 2D boxes stay intact.
            if not isinstance(value, dict):
                return value, []
            value = deepcopy(value)
            changes = []
            entities = value.get('entities')
            if isinstance(entities, list):
                for entity in entities:
                    if not isinstance(entity, dict):
                        continue
                    # Missing native linkage means unknown, not missing visual analysis.
                    # Keep invalid supplied values for validation; never infer an ID.
                    if 'sourceObjectIds' not in entity:
                        entity['sourceObjectIds'] = []
                        changes.append({'entityId': entity.get('id'), 'added': [],
                                        'reason': 'missing_native_reference'})
                    source_ids = entity.get('sourceObjectIds')
                    if not known_source_ids and isinstance(source_ids, list) and source_ids and all(isinstance(i, str) for i in source_ids):
                        changes.append({'entityId': entity.get('id'), 'removed': source_ids[:],
                                        'reason': 'no_native_object_metadata'})
                        entity['sourceObjectIds'] = []
            return value, changes

        def consider(value, record=None):
            try:
                value, changes = apply_source_id_policy(value)
                warnings = validate_mapping(value, assets, frames, scene_manifest, allow_ambiguous=True)
            except (GuideError, ValueError, TypeError, KeyError, AttributeError):
                return None
            candidates.append((value, warnings))
            if changes and record is not None:
                record['sourceObjectIdAdjustments'] = changes
            return warnings

        for previous in attempts:
            warnings = consider(previous.get("candidate"), previous)
            if warnings == []:
                bundle.update(mapping=candidates[-1][0], mappingWarnings=[], mappingStatus="ready")
                bundle['assetMappingInventory'] = assess_asset_inventory(inventory, bundle['mapping'], frames, scene_manifest)
                save()
                return
        if not attempts and frames and scene_manifest:
            camera_data = []
            for frame in frames:
                t = frame.get('time')
                matches = [row for row in scene_manifest.get('frames', []) if isinstance(row, dict)
                           and type(t) in (int, float) and type(row.get('time')) in (int, float)
                           and abs(row['time'] - t) < 1e-5]
                camera = matches[0].get('camera') if len(matches) == 1 else None
                location = camera.get('location') if isinstance(camera, dict) else None
                lens = camera.get('lens') if isinstance(camera, dict) else None
                if (not isinstance(location, list) or len(location) != 3
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in location)
                        or type(lens) not in (int, float) or not math.isfinite(lens)):
                    break
                camera_data.append({'index': frame['index'], 'position': location, 'lens': lens})
            if len(camera_data) == len(frames):
                native_mapping, adjustments = complement_mapping_from_native(
                    {'cameraSummary': 'Blender camera measurements: ' + json.dumps(camera_data, ensure_ascii=False),
                     'entities': []}, assets, frames, scene_manifest)
                try:
                    validate_mapping(native_mapping, assets, frames, scene_manifest)
                except (GuideError, ValueError, TypeError, KeyError, AttributeError):
                    pass
                else:
                    bundle.update(mapping=native_mapping, mappingWarnings=[], mappingStatus='ready',
                                  nativeMappingAdjustments=adjustments)
                    bundle['assetMappingInventory'] = assess_asset_inventory(inventory, native_mapping, frames, scene_manifest)
                    save()
                    return
        total_limit = MAPPING_REPAIR_ATTEMPTS + MAPPING_REGENERATION_ATTEMPTS
        mapping_schema = deepcopy(MAPPING_SCHEMA)
        source_id_schema = mapping_schema['properties']['entities']['items']['properties']['sourceObjectIds']
        if known_source_ids:
            source_id_schema['items']['enum'] = known_source_ids
            source_id_rules = (
                "Original 3D object metadata is supplied. sourceObjectIds may contain ONLY exact "
                "entries from knownSourceObjectIds. Never derive 3D source IDs from names, asset "
                "labels, descriptions or pixels. "
            )
        else:
            source_id_schema['maxItems'] = 0
            source_id_rules = VIDEO_ONLY_MAPPING_RULES
        frame_schema = mapping_schema["properties"]["entities"]["items"]["properties"]["frames"]
        frame_schema.update(minItems=len(frames), maxItems=len(frames))
        frame_schema["items"]["properties"]["index"]["enum"] = list(range(len(frames)))
        mapping_schema["properties"]["entities"]["items"]["properties"]["assetIndices"] = {
            "type": "array",
            "minItems": 1,
            "items": {"type": "integer", "enum": list(range(1, len(assets) + 1))},
        }
        box_schema = mapping_schema['properties']['entities']['items']['properties']['frames']['items']['properties']['bbox']
        box_schema.update(minItems=4,maxItems=4,items={'type':'integer','minimum':0,'maximum':1000})
        numbering_rules = source_id_rules + (
            "LAYOUT IMAGES ARE FIRST. Measure boxes ONLY in those layout images, never in asset images. "
            "bbox convention is [y_min,x_min,y_max,x_max], four integer coordinates on 0..1000. "
            "The server converts this explicit convention to internal normalized xyxy. "
            "Asset images follow layouts and supply identity only, not object positions. "
            "For appearance describe ONLY the referenced asset's face, clothing, anatomy and materials. "
            "Never describe a layout's gray/wooden proxy as the final asset appearance. "
            "assetIndices refer to the asset IDs listed in the contract, NOT image numbers. "
            "Map each supplied asset exactly once. Hidden objects use visible=false and [0,0,0,0]. "
            "Background/environment references are required assets too: map their intended regions "
            "as entities even when they are not a discrete foreground object. Multiple complementary "
            "references may belong to one environment entity. Verify that the union of assetIndices "
            "contains every contract asset index exactly once before returning. "
            "EVERY entity, including backgrounds and props, MUST have exactly one sample for EACH "
            "layout index listed in layoutIndices, including index 0. Never omit unchanged, hidden, "
            "or offscreen samples. Inspect each layout independently for visibility and position; "
            "do not copy another frame's box or treat missing observations as hidden. "
            "Entity id is a new stable mapping identity, distinct from sourceObjectIds. "
            "Do not invent visible boxes for hidden props. "
        )
        if analysis_sheets:
            numbering_rules += (
                ' After the layout images, asset references are ANALYSIS CONTACT SHEETS, '
                'not one image per asset. Use analysisSheetReferences to locate each numbered '
                'asset by imageNumber and bbox. assetIndices always refer to assetIndex, '
                'never contact sheet number. Preserve distinct identities. '
            )
        contract = {
            "sceneRequest": prompt if mapping_request is None else mapping_request,
            "assetIndices": list(range(1, len(assets) + 1)),
            "assets": [{"index": index, "imageNumber":len(frames)+index, "role": asset["role"],
                        "label": asset.get("label", ""), "sourceIndex": asset.get("sourceIndex")}
                       for index, asset in enumerate(assets, 1)],
            "layoutIndices": list(range(len(frames))),
            "layoutImages": [{"index":f["index"],"imageNumber":i+1,"time":f["time"]} for i,f in enumerate(frames)],
            "knownSourceObjectIds": known_source_ids,
            "mappingMode": "native_objects" if known_source_ids else "video_frames_only",
            "hiddenEntitySample": {"index": 0, "visible": False, "bbox": [0, 0, 0, 0]},
        }
        if analysis_sheets:
            contract['analysisSheetReferences'] = [{**ref, 'imageNumber': len(frames) + ref['sheetIndex']} for ref in sheet_refs]
            for asset, ref in zip(contract['assets'], sheet_refs):
                asset['imageNumber'] = len(frames) + ref['sheetIndex']
        while len(attempts) < total_limit:
            # Ambiguous but structurally usable mappings already have a warning fallback.
            if len(attempts) >= MAPPING_REPAIR_ATTEMPTS and candidates:
                break
            regenerating = len(attempts) >= MAPPING_REPAIR_ATTEMPTS
            if check_owner:
                check_owner()
            target_indices, target_frames, target_entity, prior_mapping = [], [], None, None
            if attempts and not regenerating:
                prior_mapping = attempts[-1].get('candidate')
                try:
                    validate_mapping(prior_mapping, assets, frames, scene_manifest)
                except GuideError as error:
                    prefix = '일부 에셋이 누락되거나 중복 매핑되었습니다. '
                    message = str(error)
                    if message.startswith(prefix):
                        try:
                            details = json.loads(message[len(prefix):])
                        except (ValueError, TypeError):
                            details = {}
                        if not details.get('duplicateAssetIndices'):
                            target_indices = [row['index'] for row in details.get('missingAssets', [])
                                              if type(row.get('index')) is int]
                    frame_prefix = '공간 가이드 프레임별 대상 매핑이 누락되거나 중복되었습니다. '
                    if message.startswith(frame_prefix):
                        try:
                            details = json.loads(message[len(frame_prefix):])
                        except (ValueError, TypeError):
                            details = {}
                        if not details.get('duplicateIndices') and not details.get('unexpectedIndices'):
                            target_frames = [i for i in details.get('missingIndices', []) if type(i) is int]
                            target_entity = next((e for e in prior_mapping.get('entities', [])
                                                  if e.get('id') == details.get('entityId')), None)
                            if target_entity and target_frames:
                                target_indices = target_entity.get('assetIndices', [])
                except (ValueError, TypeError, KeyError, AttributeError):
                    prior_mapping = None
                # Do not repeat the full analysis when the missing part cannot
                # be isolated safely from the previous response.
                if not target_indices:
                    break
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
            request_schema = deepcopy(mapping_schema)
            request_contract = deepcopy(contract)
            request_rules = numbering_rules
            if target_indices and isinstance(prior_mapping, dict):
                request_contract['assetIndices'] = target_indices
                request_contract['assets'] = [row for row in contract['assets'] if row['index'] in target_indices]
                if 'analysisSheetReferences' in request_contract:
                    request_contract['analysisSheetReferences'] = [row for row in request_contract['analysisSheetReferences']
                                                                  if row.get('assetIndex') in target_indices]
                request_schema['properties']['entities']['items']['properties']['assetIndices']['items']['enum'] = target_indices
                request_rules = request_rules.replace('Map each supplied asset exactly once.',
                                                      'Map each targeted asset exactly once.')
                feedback = ('\nAnalyze ONLY the missing asset indices. Return entities only for these indices; '
                            'do not repeat or change already mapped entities. '
                            'Provide a separate observation for every requested layout frame. '
                            'Do not guess visibility or positions.')
                if target_frames and target_entity:
                    request_contract['layoutIndices'] = target_frames
                    request_contract['layoutImages'] = [row for row in contract['layoutImages'] if row['index'] in target_frames]
                    frame_spec = request_schema['properties']['entities']['items']['properties']['frames']
                    frame_spec.update(minItems=len(target_frames), maxItems=len(target_frames))
                    frame_spec['items']['properties']['index']['enum'] = target_frames
                    feedback = ('\nAnalyze ONLY the missing layout frames for entity ' + target_entity['id'] +
                                '. Return that entity with the same id and assetIndices, and only the missing frame observations. '
                                'Do not guess visibility or positions.')
            feedback += "\nMANDATORY MAPPING INPUT/OUTPUT CONTRACT\n" + json.dumps(request_contract, ensure_ascii=False)
            record = {"attempt": len(attempts) + 1, "candidate": None,
                      "mode": "regenerate" if regenerating else "repair"}
            if target_indices:
                record['targetAssetIndices'] = target_indices
            if target_frames:
                record['targetFrameIndices'] = target_frames
            try:
                record["candidate"] = await self.analyze(
                    "spatial_asset_mapping", request_schema, request_rules + feedback,
                    [f["path"] for f in frames] + (list(analysis_sheets) if analysis_sheets else [Path(a["path"]) for a in assets]),
                )
                record["rawCandidate"] = deepcopy(record["candidate"])
                record["coordinateConvention"] = "yxyx_1000"
                record["candidate"] = normalize_model_mapping(record["candidate"])
                record['candidate'], changes = apply_source_id_policy(record['candidate'])
                if target_indices:
                    partial_issues = mapping_field_issues(record['candidate'])
                    if partial_issues:
                        record['fieldIssues'] = partial_issues
                        raise GuideError('에셋 매핑 필수값 오류: ' + json.dumps(partial_issues, ensure_ascii=False))
                    new_entities = record['candidate'].get('entities') if isinstance(record['candidate'], dict) else None
                    if not isinstance(new_entities, list) or any(
                        not isinstance(entity, dict) or not isinstance(entity.get('assetIndices'), list)
                        or any(type(i) is not int or i not in target_indices for i in entity['assetIndices'])
                        for entity in new_entities):
                        raise GuideError('누락 에셋 전용 응답에 기존 에셋이나 잘못된 번호가 포함되었습니다.')
                    record['partialCandidate'] = deepcopy(record['candidate'])
                    if target_frames:
                        if (len(new_entities) != 1 or new_entities[0].get('id') != target_entity.get('id')
                                or new_entities[0].get('assetIndices') != target_entity.get('assetIndices')
                                or sorted(f.get('index') for f in new_entities[0].get('frames', [])) != sorted(target_frames)):
                            raise GuideError('누락 프레임 전용 응답이 기존 대상 또는 요청 프레임과 일치하지 않습니다.')
                        merged = deepcopy(prior_mapping)
                        existing = next(e for e in merged['entities'] if e.get('id') == target_entity['id'])
                        existing['frames'].extend(deepcopy(new_entities[0]['frames']))
                        existing['frames'].sort(key=lambda row: row['index'])
                        record['candidate'] = merged
                    else:
                        record['candidate'] = {**deepcopy(prior_mapping),
                            'entities': deepcopy(prior_mapping['entities']) + deepcopy(new_entities)}
                if changes:
                    record['sourceObjectIdAdjustments'] = changes
                record['candidate'], native_changes = complement_mapping_from_native(
                    record['candidate'], assets, frames, scene_manifest)
                if native_changes:
                    record['nativeMappingAdjustments'] = native_changes
                record["fieldIssues"] = mapping_field_issues(record["candidate"])
                try:
                    validate_mapping(record["candidate"], assets, frames, scene_manifest)
                except GuideError:
                    # Some providers return an incomplete canonical list alongside
                    # a complete versioned list. Accept only an exact extension of
                    # the canonical entries, after the usual full validation.
                    raw = record['rawCandidate']
                    primary = raw.get('entities') if isinstance(raw, dict) else None
                    viable = []
                    if isinstance(primary, list) and all(isinstance(e, dict) and isinstance(e.get('id'), str) for e in primary):
                        for key, alternate in raw.items():
                            if not re.fullmatch(r'entities_v[0-9]+', key) or not isinstance(alternate, list):
                                continue
                            ids = [e.get('id') for e in alternate if isinstance(e, dict)]
                            if len(ids) != len(alternate) or len(ids) != len(set(ids)) or not all(e in alternate for e in primary):
                                continue
                            proposed = {k: deepcopy(v) for k, v in raw.items() if not re.fullmatch(r'entities_v[0-9]+', k)}
                            proposed['entities'] = deepcopy(alternate)
                            try:
                                proposed = normalize_model_mapping(proposed)
                                proposed, adjustments = apply_source_id_policy(proposed)
                                validate_mapping(proposed, assets, frames, scene_manifest)
                            except (GuideError, ValueError, TypeError, KeyError, AttributeError):
                                continue
                            viable.append((key, proposed, adjustments))
                    if len(viable) != 1:
                        raise
                    key, record['candidate'], adjustments = viable[0]
                    record['entityListNormalization'] = {'source': key, 'reason': 'complete_consistent_entity_list'}
                    if adjustments:
                        record['sourceObjectIdAdjustments'] = adjustments
                    record['fieldIssues'] = mapping_field_issues(record['candidate'])
                record["issue"] = None
            except (GuideError, ValueError, TypeError, KeyError, AttributeError, httpx.HTTPError) as error:
                if isinstance(error, httpx.HTTPStatusError):
                    record["issue"] = f"모델 요청 실패: HTTP {error.response.status_code}"
                elif isinstance(error, httpx.HTTPError):
                    record["issue"] = f"모델 통신 실패: {type(error).__name__}"
                else:
                    record["issue"] = str(error)
            if check_owner:
                check_owner()
            status_candidate = prior_mapping if target_indices and record.get('issue') else record.get('candidate')
            bundle['assetMappingInventory'] = assess_asset_inventory(
                inventory, status_candidate, frames, scene_manifest)
            record['assetMappingStatus'] = [
                {'index': item['index'], 'registrationStatus': item['registrationStatus'],
                 'linkStatus': item['linkStatus'], 'analysisStatus': item['analysisStatus']}
                for item in bundle['assetMappingInventory']]
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
            bundle['assetMappingInventory'] = assess_asset_inventory(inventory, mapping, frames, scene_manifest)
            save()
            logging.getLogger("uvicorn.error").info(
                "[asset-mapping] 보정 3회 완료 · 보정 필요 %d건 · 이미지 생성 계속", len(warnings),
            )
            return
        bundle["mappingStatus"] = "unresolved"
        error_attempts = [
            {"attempt": attempt.get("attempt", index + 1),
             "mode": attempt.get("mode"),
             "issue": str(attempt.get("issue") or "필수 매핑 데이터 누락")[:1200]}
            for index, attempt in enumerate(attempts)
        ]
        bundle["mappingErrorSummary"] = {"attempts": error_attempts}
        save()
        attempt_details = " / ".join(
            f"{item['attempt']}차: {item['issue']}" for item in error_attempts
        ) or "검증 결과가 없습니다."
        raise GuideError(
            f"에셋 매핑을 {len(error_attempts)}회 시도했으나 완료하지 못했습니다. "
            f"{attempt_details}. 상세 정보는 mappingAttempts를 확인하세요."
        )

    async def detect_asset_parts(self, assets, *, on_attempt=None):
        characters = [
            (index, asset) for index, asset in enumerate(assets, 1) if asset["role"] == "character"
        ]
        analysis = {"sheets": []}
        if not characters:
            return character_asset_parts(assets, analysis)
        layout_hints = []
        for index, asset in characters:
            try:
                with Image.open(asset['path']) as image:
                    hint = detect_sheet_layout(image)
            except (OSError, ValueError):
                hint = None
            if hint:
                layout_hints.append({'imageIndex': index, **hint})
        prompt = (
                "Inspect these character references for structured sheets of DIFFERENT named or visually distinct people. "
                "Return one sheets entry per input, using these original imageIndex values in order: "
                + json.dumps([index for index, _ in characters])
                + ". "
                "For a multi-person sheet locate one non-overlapping rectangular panel per distinct person. "
                "Inspect rows as well as columns: a top row with one person's front/back/face views "
                "and a bottom row with another person's views is TWO members. Each member's panel "
                "must include their entire row, not just a face or one turnaround view. "
                "Also inspect vertical columns: a column containing one person's portrait, front/back "
                "outfits and details is ONE member, and adjacent columns with distinct people are separate members. "
                "Choose row or column grouping from the content, not the image aspect ratio. "
                "Include their face and full-body turnaround views together in their panel. Use the label printed "
                "for that person when readable. Front, side, back and detail views of one person are a single identity. "
                "For a single-person sheet or a group photo without separable panels return an empty members list. "
                "bbox is [left, top, right, bottom] relative to the full input image in the range 0..1. "
                "Use normalized fractions, never pixel or 0..1000 coordinates. "
                "Labels must be nonempty and unique within each sheet; use distinct descriptive labels "
                "when there are no printed names."
                " Python detected the following regular divider candidates (xyxy normalized coordinates): "
                + json.dumps(layout_hints, ensure_ascii=False)
                + ". These are layout hints, not confirmed people: verify each region visually and group "
                  "multiple views of the same identity. Do not treat outfit details as extra people."
        )
        feedback = ''
        for attempt in range(1, 3):
            analysis = await self.analyze(
                "spatial_character_sheet", CAST_SHEET_SCHEMA, prompt + feedback,
                [Path(asset['path']) for _, asset in characters],
            )
            raw_analysis = deepcopy(analysis)
            analysis = normalize_character_sheet_indices(analysis, characters)
            record = {'attempt': attempt, 'rawCandidate': raw_analysis, 'candidate': deepcopy(analysis), 'issue': None}
            try:
                parts = character_asset_parts(assets, analysis)
            except (GuideError, ValueError, TypeError, KeyError, AttributeError) as error:
                record['issue'] = str(error)
                if on_attempt:
                    on_attempt(record)
                if attempt == 2:
                    raise GuideError('캐릭터 통합 시트 분석 2회 실패: ' + str(error)) from error
                feedback = ('\nCorrect the previous sheet analysis using the same images. '
                            'Do not merge different people or guess missing image regions. Error: '
                            + str(error) + '\nPrevious response: '
                            + json.dumps(analysis, ensure_ascii=False))
                continue
            record['layoutComparison'] = compare_sheet_layout(analysis, layout_hints)
            for comparison in record['layoutComparison']:
                if comparison['status'] == 'layout_hint_difference':
                    logging.getLogger('uvicorn.error').info(
                        '[캐릭터 시트] 구분선 영역과 인물 분석 차이 · 이미지 %s · 영역 %s개 · 인물 %s · %s',
                        comparison['imageIndex'], comparison['detectedRegionCount'],
                        comparison['identityCount'] if comparison['identityCount'] is not None else '단일 또는 미분리',
                        '분석된 인물 영역 유지' if comparison['identityCount'] is not None else '원본 이미지 유지')
            if on_attempt:
                on_attempt(record)
            empty_indices = {sheet['imageIndex'] for sheet in analysis['sheets'] if not sheet['members']}
            if empty_indices and attempt == 1:
                first_analysis = deepcopy(analysis)
                empty_indices_first = empty_indices
                feedback = (
                    '\nRECHECK all images with empty members, imageIndex=' + json.dumps(sorted(empty_indices))
                    + '. Count distinct faces and identities. Independently inspect entire rows AND columns, '
                    'including face portraits paired with headless front/back outfits and detail panels. '
                    'A row or column is one person; multiple views of the same person remain one identity. '
                    'Return empty members only when the image contains one person or no separable panels. '
                    'Return the complete sheets list for all inputs.'
                )
                continue
            if attempt == 2 and 'first_analysis' in locals():
                # Only reconsider sheets initially classified as single. Preserve
                # confirmed multi-person panels from the first independent pass.
                revised = {sheet['imageIndex']: sheet for sheet in analysis['sheets']}
                merged = {'sheets': [revised[sheet['imageIndex']] if sheet['imageIndex'] in empty_indices_first
                                     else sheet for sheet in first_analysis['sheets']]}
                parts = character_asset_parts(assets, merged)
            return parts

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
        opening_state="",
        mapping_request=None,
    ):
        generation_limit = ENDPOINT_GENERATION_ATTEMPTS if input_format in ("first_frame", "first_last") else GUIDE_GENERATION_ATTEMPTS
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise GuideError("공간 가이드 저장 경로가 올바르지 않습니다.")
        if not assets:
            raise GuideError("공간 가이드를 만들 캐릭터·배경 에셋을 등록하세요.")
        signature = {
            "version": GUIDE_VERSION,
            "openingState": opening_state,
            "mappingRequest": mapping_request,
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
        if 'inputAssetInventory' not in bundle:
            bundle['inputAssetInventory'] = build_asset_inventory(original_assets)
            save()
        if "assetParts" not in bundle and "mapping" not in bundle:
            def save_sheet_attempt(record):
                bundle.setdefault('assetPartAttempts', []).append(record)
                save()

            bundle["assetParts"] = await self.detect_asset_parts(assets, on_attempt=save_sheet_attempt)
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
            analysis_sheets, sheet_refs = await asyncio.to_thread(create_analysis_sheets, assets, directory)
            if len(frames) + len(analysis_sheets) > 4:
                analysis_sheets, sheet_refs = await asyncio.to_thread(create_analysis_sheets, assets, directory, combined=True)
            if len(frames) + len(analysis_sheets) > 4:
                raise GuideError('에셋 매핑 프레임이 분석 이미지 4장 한도를 초과합니다. 가이드 수를 줄여 주세요.')
            bundle['analysisSheetReferences'] = sheet_refs
            save()
            await self.repair_mapping(
                bundle, prompt,
                [Path(asset["path"]) for asset in assets]
                + [frame["path"] for frame in frames]
                + [frame["path"] for frame in timeline],
                assets, frames, scene_manifest, save, check_owner,
                mapping_request=brief if mapping_request is None else mapping_request,
                analysis_sheets=analysis_sheets, sheet_refs=sheet_refs,
            )
        validate_mapping(bundle["mapping"], assets, frames, scene_manifest,
                         allow_ambiguous=bool(bundle.get("mappingWarnings")))
        bundle['assetMappingInventory'] = assess_asset_inventory(
            build_asset_inventory(assets), bundle['mapping'], frames, scene_manifest)
        unresolved = [{'index': row['index'], 'assetId': row['assetId'],
                       'registrationStatus': row['registrationStatus'],
                       'linkStatus': row['linkStatus'], 'analysisStatus': row['analysisStatus']}
                      for row in bundle['assetMappingInventory']
                      if row['registrationStatus'] != 'registered' or row['linkStatus'] != 'linked'
                      or row['analysisStatus'] != 'complete']
        if unresolved:
            bundle['mappingStatus'] = 'ready_with_warnings'
            bundle['mappingQualityWarnings'] = {'unresolvedAssets': unresolved}
            save()
            logging.getLogger('uvicorn.error').warning('[asset-mapping] 연결·공간 분석 조건 미달 · 경고 기록 후 계속 진행 · %s',
                                                       json.dumps(unresolved, ensure_ascii=False))
        save()
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
                             if item["index"] == index - 1 and item.get("status") in ("verified", "invalid", "excluded")
                             and item.get("sha256")), None)
            previous_path = directory / previous["name"] if previous else None
            if previous_path is not None and (
                not previous_path.is_file()
                or hashlib.sha256(previous_path.read_bytes()).hexdigest() != previous.get("sha256")
            ):
                raise GuideError("직전 씬의 공간 가이드 이미지가 변경되거나 누락되었습니다.")
            while entry["status"] not in ("excluded", "unknown", "requesting"):
                if (
                    entry["status"] == "invalid"
                    and entry.get("generationAttempts", 1) >= generation_limit
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
                                    for key in ("id", "label", "assetIndices")
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
                        entity['assetReferences'] = [
                            {'image': image_number, 'role': role}
                            for image_number, role in sorted({
                                (edit_indices[value], assets[value - 1]['role'])
                                for value in entity['assetIndices']
                            })
                        ]
                        entity["editImageNumbers"] = sorted(
                            {edit_indices[value] for value in entity.pop("assetIndices")}
                        )
                    prompt = (
                        "Edit Image 1 as an exact camera-layout reconstruction at this SINGLE instant. "
                        "Resolve input conflicts by these separate authorities, before considering scene descriptions: "
                        "1. ASSET APPEARANCE: The numbered asset references are authoritative for identity, face, hair, clothing, anatomy and materials. "
                        "Rebuild each visible entity from its assetReferences/editImageNumbers. Image 1 supplies NO final character appearance. "
                        "Replace its neutral proxy bodies completely; do not retain proxy geometry with a texture applied. "
                        "2. LAYOUT GEOMETRY: Image 1 alone is authoritative for camera viewpoint, framing, facing direction, subject centers, "
                        "approximate bounding-box scale, poses, supports, contact and occlusion. "
                        "Never copy the asset images' subject scale, position or pose. "
                        "Use background asset images for environment appearance and furnishings, but NEVER adopt a background asset camera "
                        "or reverse the view to match its composition. Preserve Image 1's front/side/back view. "
                        "3. SCENE STATE: Use sceneBrief for location and action at this timestamp within those appearance and layout constraints. "
                        "Keep a perched animal perched if perched in Image 1. Do not execute later actions or invent a more dramatic composition. "
                        "Maintain connected indoor/outdoor geography and persistent doorway, wall and prop structures across endpoints. "
                        "An exterior appearance reference can establish the adjacent building while the current layout remains indoors. "
                        "The normalized bbox for each entity is a spatial constraint: retain its screen center and approximate extent with natural asset proportions. "
                        "Rebuild this instant's environment from sceneBrief, replacing neutral proxy backgrounds. "
                        "Only listed visible entities may appear. Excluded/offscreen entities MUST remain absent, even when in the overall story. "
                        "No panels, labels, text annotations or reference sheets. "
                        "Snapshot constraints: "
                        + json.dumps(
                            {
                                "time": frame["time"],
                                "sceneBrief": scene_brief_for_frame(index, opening_state, brief),
                                "cameraSummary": "Use the exact camera of Image 1.",
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
                        verify_sheets, verify_refs = await asyncio.to_thread(create_analysis_sheets, assets, directory)
                        if 2 + len(verify_sheets) + int(previous_path is not None) > 4:
                            verify_sheets, verify_refs = await asyncio.to_thread(create_analysis_sheets, assets, directory, combined=True)
                        verification, verification_attempts = await verify_guide_response(
                            self.analyze,
                            "Verify the edited guide: first image=rough layout, next "
                            + str(len(verify_sheets))
                            + "=numbered asset contact sheets, then an optional previous-scene reference, final image=generated spatial guide. "
                            "Contact sheet regions and source references: " + json.dumps(verify_refs, ensure_ascii=False) + ". "
                            "Check only entities expected visible in this frame; preserve hidden/offscreen state. Judge asset identity/proportions/materials, camera composition/positions, removal of proxy geometry, and intended cast count. "
                            "Use asset images as the appearance authority and the source layout only as the geometry/camera authority; mapping appearance text cannot override asset images. "
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
                                 "sceneBrief": scene_brief_for_frame(index, opening_state, brief), "previousSceneIncluded": previous_path is not None,
                                 "previousSceneIndex": previous["index"] if previous else None},
                                ensure_ascii=False,
                            ),
                            [frame["path"]] + list(verify_sheets)
                            + ([previous_path] if previous_path else [])
                            + [target], model=self.model, index=index,
                        )
                        entry['verificationAttempts'] = verification_attempts
                    except (httpx.HTTPError, RuntimeError) as error:
                        diagnostic = guide_verification_diagnostic(error, model=self.model, index=index)
                        if getattr(error, 'verification_attempts', None):
                            entry['verificationAttempts'] = error.verification_attempts
                        entry.update(status="excluded", verificationIssue=type(error).__name__, verificationDiagnostic=diagnostic)
                        logging.getLogger('uvicorn.error').warning('[spatial-guide-verification] 검증 실패 · %s', json.dumps(diagnostic, ensure_ascii=False))
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
                    if not valid:
                        checks = ('assetIdentityPreserved', 'cameraCompositionPreserved', 'proxyShapesRemoved',
                                  'castCountPreserved', 'locationContinuityPreserved', 'sceneStatePreserved')
                        diagnostic = {'stage': 'guide_verification', 'index': index, 'model': self.model,
                                      'code': 'verification_rejected',
                                      'failedFields': [key for key in checks if verification.get(key) is False],
                                      'missingFields': [key for key in checks if key not in verification],
                                      'invalidFields': [key for key in checks if key in verification and type(verification[key]) is not bool]}
                        entry['verificationDiagnostic'] = diagnostic
                        logging.getLogger('uvicorn.error').warning('[spatial-guide-verification] 검증 불일치 · %s', json.dumps(diagnostic, ensure_ascii=False))
                    else:
                        entry.pop('verificationDiagnostic', None)
                    save()
                    if not valid:
                        break
                break
        bundle["status"] = (
            "ready"
            if all(guide["status"] == "verified" for guide in bundle["guides"])
            else "partial"
        )
        save()
        return bundle

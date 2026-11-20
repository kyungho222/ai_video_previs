"""Request metadata shared by the scenario, previs and video stages."""
from ai_video.model_gateway import canonical_model

# Temporary override. Set False to restore frontend-selected chat models.
FORCE_DEFAULT_CHAT_MODEL = False


def effective_chat_model(service, requested):
    if FORCE_DEFAULT_CHAT_MODEL:
        return (getattr(service, "default_model", None)
                or service.config.get("PREVIS_DEFAULT_CHAT_MODEL", "gpt-6.1-terra"))
    return canonical_model(requested)


def request_work_metadata(body):
    metadata = {key: body[key] for key in ("db_name", "chatbotid") if key in body}
    if "db_name" in metadata and not isinstance(metadata["db_name"], str):
        raise ValueError("db_name은 문자열이어야 합니다.")
    if "chatbotid" in metadata and type(metadata["chatbotid"]) not in (str, int):
        raise ValueError("chatbotid는 문자열 또는 정수여야 합니다.")
    return metadata


def video_analysis_model(service, options):
    if FORCE_DEFAULT_CHAT_MODEL:
        return effective_chat_model(service, None)
    settings = options.get("workSettings") or {}
    return canonical_model(settings.get("model") or service.config.get("VIDEO_PROMPT_MODEL")
                           or getattr(service, "default_model", "gpt-6.1-terra"))

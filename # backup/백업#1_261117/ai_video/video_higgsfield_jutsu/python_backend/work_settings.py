"""Request metadata shared by the scenario, previs and video stages."""


def request_work_metadata(body):
    metadata = {key: body[key] for key in ("db_name", "chatbotid") if key in body}
    if "db_name" in metadata and not isinstance(metadata["db_name"], str):
        raise ValueError("db_name은 문자열이어야 합니다.")
    if "chatbotid" in metadata and type(metadata["chatbotid"]) not in (str, int):
        raise ValueError("chatbotid는 문자열 또는 정수여야 합니다.")
    return metadata


def video_analysis_model(service, options):
    settings = options.get("workSettings") or {}
    return (settings.get("model") or service.config.get("VIDEO_PROMPT_MODEL")
            or getattr(service, "default_model", "gpt-6.1-terra"))

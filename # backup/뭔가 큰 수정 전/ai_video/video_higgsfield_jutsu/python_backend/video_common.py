"""Compatibility exports for existing video_common callers."""

from .video_errors import (
    PromptLengthError,
    MAX_REFERENCE_BYTES,
    VideoError,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    ROLES,
    ASSET_ID,
    AssetError,
    UUID,
    MAX_FORM_OVERHEAD,
)

from .video_media import (
    PublicNetworkBackend,
    PublicHTTPTransport,
    public_https,
    inspect_video,
    VideoAssets,
    LimitedUploadRoute,
    UploadedPrevis,
    create_previs_upload_router,
    download_video,
)

from .job_repository import (
    VideoJobs,
)

from .video_prompt import (
    PROMPT_POLICY_VERSION,
    TASK,
    PREVIS_RULES,
    CAMERA_GUIDE_RULES,
    INDEPENDENT_REACTION_RULES,
    ASSET_REBUILD_RULES,
    CONTINUITY_RULES,
    SPEECH_LANGUAGE_RULES,
    MOTION_RULES,
    build_video_prompt,
    build_model_video_prompt,
    DEFAULT_VIDEO_PROMPT,
    _LEGACY_DEFAULTS,
    timeline_duration,
    dog_mirror_scene,
    clean_scene_text,
    normalize_video_request,
    build_reference_prompt,
    reference_prompt_overhead,
    optimize_video_prompt,
    fit_video_prompt,
)

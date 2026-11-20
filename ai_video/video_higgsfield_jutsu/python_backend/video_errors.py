"""Shared video input errors and limits."""

import re


class PromptLengthError(ValueError):
    """The final prompt exceeds the provider budget; never truncate silently."""



MAX_REFERENCE_BYTES = 100 * 1024 * 1024



class VideoError(RuntimeError):
    """Only user-safe messages may cross the HTTP boundary."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code



MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 15
MAX_AUDIO_CLIPS = 5
AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}



MAX_IMAGES = 10



ROLES = {
    "character": "character appearance",
    "background": "background and environment",
    "other": "props and visual style",
}



ASSET_ID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")



class AssetError(ValueError):
    pass



UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"



MAX_FORM_OVERHEAD = 64 * 1024


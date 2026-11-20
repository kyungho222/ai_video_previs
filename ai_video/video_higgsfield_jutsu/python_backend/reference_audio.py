"""Prepare reference audio within the video model's total playback budget."""

import asyncio
from math import floor
from pathlib import Path
from tempfile import TemporaryDirectory

from .video_errors import MAX_AUDIO_SECONDS, VideoError


async def upload_reference_audio(client, assets, selected, region):
    if not selected:
        return []

    clips, limits = audio_clips(assets, selected)
    urls = []
    with TemporaryDirectory(prefix="video-audio-") as directory:
        for index, ((asset, path), limit) in enumerate(zip(clips, limits)):
            media_type = asset["mediaType"]
            if asset["duration"] != limit:
                trimmed = Path(directory) / f"audio-{index + 1}.wav"
                await trim_audio(path, trimmed, limit)
                path, media_type = trimmed, "audio/wav"
            urls.append(await client.upload(path, region, media_type=media_type))
    return urls


def audio_clips(assets, selected):
    clips = [assets.get(asset["id"]) for asset in selected]
    lengths = [max(1, asset["duration"]) for asset, _ in clips]
    remaining = MAX_AUDIO_SECONDS
    limits = lengths.copy()
    shortest_first = sorted(range(len(clips)), key=lengths.__getitem__)
    for offset, index in enumerate(shortest_first):
        limits[index] = min(lengths[index], remaining / (len(clips) - offset))
        remaining -= limits[index]

    return clips, limits


async def trim_audio(source, output, seconds):
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source),
            "-map", "0:a:0", "-af", "apad=whole_dur=1", "-t",
            f"{floor(seconds * 1000) / 1000:.3f}",
            "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise VideoError("참고 음원을 편집하려면 서버에 FFmpeg가 필요합니다.") from error
    try:
        if await process.wait():
            raise VideoError("참고 음원의 앞부분을 추출하지 못했습니다.")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

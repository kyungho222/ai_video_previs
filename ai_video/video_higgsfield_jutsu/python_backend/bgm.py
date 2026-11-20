"""Mix attached background music into the completed video."""

import asyncio

from .reference_audio import audio_clips
from .video_errors import VideoError


VOICE_ONLY = (
    "Audio: generate only the requested voices, dialogue, or narration. "
    "Do not generate music, instruments, sound effects, or ambient audio."
)


async def _run_audio_tool(*args):
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise VideoError("배경음악을 합성하려면 서버에 FFmpeg와 FFprobe가 필요합니다.") from error
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 120)
    except TimeoutError as error:
        raise VideoError("배경음악 합성 시간이 초과됐습니다.") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise VideoError("영상에 배경음악을 합성하지 못했습니다.")
    return stdout


async def mix_background_music(candidate, assets, selected):
    clips, limits = audio_clips(assets, selected)
    streams = await _run_audio_tool(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(candidate),
    )
    inputs = []
    filters = []
    for index, ((_, path), seconds) in enumerate(zip(clips, limits), start=1):
        inputs.extend(("-i", str(path)))
        filters.append(
            f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"atrim=end_sample={int(seconds * 48000)},asetpts=PTS-STARTPTS[m{index}]"
        )
    tracks = "".join(f"[m{index}]" for index in range(1, len(clips) + 1))
    filters.append(f"{tracks}concat=n={len(clips)}:v=0:a=1,apad[bgm]")
    if streams.strip():
        filters.extend((
            "[0:a:0]aresample=48000,aformat=channel_layouts=stereo,volume=0.8,apad[voice]",
            "[voice][bgm]amix=inputs=2:normalize=0,alimiter=limit=0.95:level=0[mixed]",
        ))
    output = candidate.with_suffix(".bgm.mp4")
    try:
        await _run_audio_tool(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(candidate), *inputs,
            "-filter_complex", ";".join(filters), "-map", "0:v:0",
            "-map", "[mixed]" if streams.strip() else "[bgm]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart",
            str(output),
        )
        output.replace(candidate)
    finally:
        output.unlink(missing_ok=True)

import json
from array import array
import math
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
import wave
import httpx
from ai_video.video_higgsfield_jutsu.python_backend.bgm import mix_background_music
from ai_video.video_higgsfield_jutsu.python_backend.wan import WanClient
from ai_video.video_higgsfield_jutsu.python_backend.reference_audio import upload_reference_audio
from ai_video.video_higgsfield_jutsu.python_backend.video_errors import MAX_AUDIO_BYTES, AssetError, VideoError
from ai_video.video_higgsfield_jutsu.python_backend.video_media import VideoAssets


def wav_clip(seconds):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(16000 * seconds))
    return buffer.getvalue()


class AudioAssetTests(unittest.TestCase):
    def test_audio_storage_limits_size_without_limiting_duration(self):
        with TemporaryDirectory() as root:
            assets = VideoAssets(root, max_images=1)
            first = assets.save(wav_clip(7), "audio", "first.wav")
            second = assets.save(wav_clip(8), "audio", "second.wav")
            third = assets.save(wav_clip(16), "audio", "long.wav")
            self.assertEqual(first["mediaType"], "audio/wav")
            self.assertEqual(assets.get(first["id"])[1].read_bytes(), wav_clip(7))
            self.assertEqual(assets.resolve([second["id"], first["id"], third["id"]]), [second, first, third])
            with self.assertRaisesRegex(AssetError, "10MB"):
                assets.save(bytes(MAX_AUDIO_BYTES + 1), "audio", "oversized.wav")
            with self.assertRaisesRegex(AssetError, "유효한 음원"):
                assets.save(b"not audio", "audio", "invalid.mp3")

class WanAudioTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    async def test_attached_music_remains_audible_with_loud_generated_audio(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            music = root / "music.wav"
            candidate = root / "video.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=16000:duration=2", str(music),
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=s=64x64:r=24:d=2", "-f", "lavfi", "-i",
                "sine=frequency=880:sample_rate=48000:duration=2",
                "-af", "volume=4", "-c:v", "libx264", "-c:a", "aac",
                "-t", "2", str(candidate),
            ], check=True, capture_output=True)
            assets = VideoAssets(root / "assets")
            asset = assets.save(music.read_bytes(), "audio", "music.wav")
            await mix_background_music(candidate, assets, [asset])
            samples = array("f", subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(candidate), "-vn",
                "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
            ]))[8000:24000]

            def amplitude(frequency):
                real = sum(x * math.cos(2 * math.pi * frequency * i / 16000)
                           for i, x in enumerate(samples))
                imag = sum(x * math.sin(2 * math.pi * frequency * i / 16000)
                           for i, x in enumerate(samples))
                return 2 * math.hypot(real, imag) / len(samples)

            # Prioritize uploaded music over generated audio, relative to each input.
            self.assertGreater(amplitude(440), 0.10)
            self.assertGreater(amplitude(880), 0.35)
            self.assertGreater(amplitude(440) / 0.125, amplitude(880) / 0.5)

    async def test_bgm_mix_keeps_video_stream_and_limits_music_to_fifteen_seconds(self):
        with TemporaryDirectory() as root:
            assets = VideoAssets(root)
            music = assets.save(wav_clip(20), "audio", "music.wav")
            candidate = Path(root) / "candidate.mp4"
            candidate.write_bytes(b"original")

            async def audio_tool(*args):
                if args[0] == "ffprobe":
                    return b"1\n"
                graph = args[args.index("-filter_complex") + 1]
                self.assertIn("atrim=end_sample=720000", graph)
                self.assertIn("[voice][bgm]amix", graph)
                self.assertEqual(args[args.index("-c:v") + 1], "copy")
                Path(args[-1]).write_bytes(b"mixed")

            with patch(mix_background_music.__module__ + "._run_audio_tool", side_effect=audio_tool):
                await mix_background_music(candidate, assets, [music])
            self.assertEqual(candidate.read_bytes(), b"mixed")
            self.assertEqual(assets.get(music["id"])[1].read_bytes(), wav_clip(20))

    async def test_upload_trims_from_start_and_shares_total_budget(self):
        with TemporaryDirectory() as root:
            assets = VideoAssets(root)
            long = assets.save(wav_clip(30), "audio", "long.wav")
            short = assets.save(wav_clip(3), "audio", "short.wav")
            client = AsyncMock()
            with patch(upload_reference_audio.__module__ + ".trim_audio", new_callable=AsyncMock) as trim:
                await upload_reference_audio(client, assets, [long], "singapore")
                self.assertEqual(trim.await_args.args[2], 15)
                trim.reset_mock()
                client.upload.reset_mock()
                await upload_reference_audio(client, assets, [long, short], "singapore")
                source, trimmed, seconds = trim.await_args.args
                self.assertEqual(source, assets.get(long["id"])[1])
                self.assertEqual(seconds, 12)
                self.assertEqual(
                    [call.args[0] for call in client.upload.await_args_list],
                    [trimmed, assets.get(short["id"])[1]],
                )
                self.assertEqual(source.read_bytes(), wav_clip(30))

    async def test_reference_audio_is_sent_only_with_supported_inputs(self):
        captured = []

        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"output": {"task_id": "audio-test-task"}})

        client = WanClient("test-key", transport=httpx.MockTransport(handle))
        audio = "oss://uploads/sound.mp3"
        await client.submit("oss://uploads/previs.mp4", "Act to Audio 1.", 10, "480P",
                            "singapore", reference_audio=[audio])
        self.assertEqual(captured[0]["input"]["media"][-1],
                         {"type": "reference_audio", "url": audio})
        with self.assertRaises(VideoError):
            await client.submit(None, "Act.", 10, "480P", "singapore",
                                reference_mode="spatial", spatial_input="first_last",
                                reference_audio=[audio])
        self.assertEqual(len(captured), 1)

    async def test_audio_enabled_for_reference_and_endpoint_inputs(self):
        captured=[]
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json={'output':{'task_id':'audio-test-task'}})
        client=WanClient('test-key',transport=httpx.MockTransport(handle))
        for mode,extra in [('images',{'reference_images':[{'url':'https://example.com/asset.png','role':'character'}]}),
                           ('video',{}),
                           ('spatial',{'spatial_input':'first_last','guide_frames':[{'url':'https://example.com/start.png','time':0},{'url':'https://example.com/end.png','time':10}]})]:
            with self.subTest(mode=mode):
                await client.submit('https://example.com/previs.mp4' if mode=='video' else None,
                                    'Sing naturally.',10,'480P','singapore',reference_mode=mode,
                                    final_prompt='Sing naturally with audible vocals.',**extra)
                self.assertIs(captured[-1]['parameters']['audio'],True)
        self.assertEqual([m['type'] for m in captured[-1]['input']['media']],['first_frame','last_frame'])

if __name__=='__main__':unittest.main()

import json
import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx

from app.adapters import MlxAudioTtsAdapter, create_tts_adapter
from app.config import Settings
from app.domain.errors import ProviderError
from app.domain.types import SegmentInput
from app.voices import CloneVoiceStore


def _wav_bytes(duration_ms: int = 100) -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * (24000 * duration_ms // 1000))
    return stream.getvalue()


class MlxAudioTtsAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_dir.cleanup)
        self.settings = Settings(
            app_password="test",
            data_dir=Path(self.data_dir.name),
            mlx_audio_base_url="http://127.0.0.1:8100",
            mlx_audio_chunk_target_chars=10,
            mlx_audio_chunk_max_chars=15,
            _env_file=None,
        )
        source = Path(self.data_dir.name) / "reference-source.wav"
        source.write_bytes(_wav_bytes(duration_ms=5000))
        self.voice = CloneVoiceStore(self.settings.clone_voices_dir).create(
            "测试音色", "这是参考音频的原话。", source
        )

    def test_splits_requests_and_merges_one_canonical_wav(self) -> None:
        requests: list[dict[str, object]] = []

        def respond(url, **kwargs):
            requests.append({"url": url, **kwargs["json"]})
            return httpx.Response(200, content=_wav_bytes(), request=httpx.Request("POST", url))

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.adapters.httpx.post", side_effect=respond
        ):
            output_path = Path(directory) / "segment.wav"
            artifact = MlxAudioTtsAdapter(self.settings).synthesize(
                SegmentInput(
                    id=7,
                    chapter_id=2,
                    segment_index=3,
                    text="第一句话用于测试。第二句话继续测试。第三句话完成测试。",
                ),
                "旁白指令",
                self.voice.id,
                output_path,
            )

            self.assertGreater(len(requests), 1)
            self.assertEqual(artifact.path, output_path)
            self.assertGreater(artifact.duration_ms, 0)
            with wave.open(str(output_path), "rb") as result:
                self.assertEqual(result.getframerate(), 24000)
                self.assertEqual(result.getnchannels(), 1)
            for request in requests:
                self.assertEqual(request["url"], "http://127.0.0.1:8100/v1/audio/speech")
                self.assertEqual(request["model"], self.settings.mlx_audio_tts_model)
                self.assertEqual(request["ref_audio"], str(self.voice.audio_path.resolve()))
                self.assertEqual(request["ref_text"], "这是参考音频的原话。")
                self.assertNotIn("voice", request)
                self.assertNotIn("instruct", request)
                self.assertEqual(request["lang_code"], "chinese")
                self.assertEqual(request["response_format"], "wav")
                self.assertIs(request["stream"], False)
                self.assertEqual(request["max_tokens"], 4096)

    def test_unknown_clone_voice_fails_before_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.adapters.httpx.post") as post:
            with self.assertRaisesRegex(ProviderError, "找不到克隆音色") as raised:
                MlxAudioTtsAdapter(self.settings).synthesize_preview(
                    "试听内容", "Uncle_Fu", Path(directory) / "segment.wav"
                )
            self.assertFalse(raised.exception.retryable)
            post.assert_not_called()

    def test_server_error_is_not_retryable_and_records_context(self) -> None:
        response = httpx.Response(
            400,
            json={"detail": "Reference audio file not found"},
            request=httpx.Request("POST", "http://127.0.0.1:8100/v1/audio/speech"),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.adapters.httpx.post", return_value=response
        ):
            output_path = Path(directory) / "segment.wav"
            with self.assertRaises(ProviderError) as raised:
                MlxAudioTtsAdapter(self.settings).synthesize_preview(
                    "试听内容", self.voice.id, output_path
                )

            self.assertFalse(raised.exception.retryable)
            diagnostic_path = Path(raised.exception.response_path)
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["provider"], "mlx_audio")
            self.assertEqual(diagnostic["chunk_index"], 1)
            self.assertEqual(diagnostic["status_code"], 400)

    def test_factory_does_not_fallback_to_another_provider(self) -> None:
        self.settings.mimo_api_key = ""
        self.assertIsInstance(create_tts_adapter("mlx_audio", self.settings), MlxAudioTtsAdapter)
        with self.assertRaisesRegex(ProviderError, "不支持的 TTS 供应商"):
            create_tts_adapter("unknown", self.settings)


if __name__ == "__main__":
    unittest.main()

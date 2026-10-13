import json
import re
import shutil
import subprocess
import uuid
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class VoiceOption:
    provider: str
    id: str
    name: str
    description: str
    preview_text: str
    cloned: bool = False


VOICE_PREVIEW_TEXT = "你好，我是你的有声书朗读助手。夜色渐深，故事才刚刚开始。"
VOICE_PREVIEW_TEXT_EN = "Hello, I'm your audiobook narrator. The night is quiet, and the story is just beginning."


def _preset(provider: str, voice_id: str, description: str, preview_text: str) -> VoiceOption:
    return VoiceOption(provider, voice_id, voice_id, description, preview_text)


MIMO_VOICES = (
    _preset("mimo", "冰糖", "中文 · 女声 · 活泼少女", VOICE_PREVIEW_TEXT),
    _preset("mimo", "茉莉", "中文 · 女声 · 知性女声", VOICE_PREVIEW_TEXT),
    _preset("mimo", "苏打", "中文 · 男声 · 阳光少年", VOICE_PREVIEW_TEXT),
    _preset("mimo", "白桦", "中文 · 男声 · 成熟男声", VOICE_PREVIEW_TEXT),
    _preset("mimo", "Mia", "English · Female · Lively girl", VOICE_PREVIEW_TEXT_EN),
    _preset("mimo", "Chloe", "English · Female · Sweet dreamy", VOICE_PREVIEW_TEXT_EN),
    _preset("mimo", "Milo", "English · Male · Sunny boy", VOICE_PREVIEW_TEXT_EN),
    _preset("mimo", "Dean", "English · Male · Steady gentle", VOICE_PREVIEW_TEXT_EN),
)

# 参考音频时长范围：过短克隆不稳定，过长会拖慢每次请求的预填充
CLONE_MIN_SECONDS = 3
CLONE_MAX_SECONDS = 20
CLONE_SAMPLE_RATE = 24000


@dataclass(frozen=True)
class ClonedVoice:
    id: str
    name: str
    ref_text: str
    duration_ms: int
    created_at: str
    audio_path: Path

    def option(self, provider: str) -> VoiceOption:
        seconds = round(self.duration_ms / 1000)
        return VoiceOption(
            provider, self.id, self.name, f"克隆音色 · 参考 {seconds} 秒", VOICE_PREVIEW_TEXT, cloned=True
        )


class CloneVoiceError(ValueError):
    pass


class CloneVoiceStore:
    """克隆音色以目录形式保存：<root>/<id>/reference.wav + meta.json。"""

    def __init__(self, root: Path):
        self.root = root

    def list(self) -> list[ClonedVoice]:
        if not self.root.is_dir():
            return []
        voices = [self._load(path) for path in self.root.iterdir() if (path / "meta.json").is_file()]
        return sorted(voices, key=lambda voice: voice.created_at)

    def get(self, voice_id: str) -> ClonedVoice | None:
        # 音色 ID 只能是创建时生成的 uuid hex，防止拼出音色目录以外的路径
        if not re.fullmatch(r"[0-9a-f]{32}", voice_id):
            return None
        directory = self.root / voice_id
        if not (directory / "meta.json").is_file():
            return None
        return self._load(directory)

    def create(self, name: str, ref_text: str, source_path: Path) -> ClonedVoice:
        name = name.strip()
        ref_text = ref_text.strip()
        if not name:
            raise CloneVoiceError("音色名称不能为空")
        if not ref_text:
            raise CloneVoiceError("请填写参考音频中说的原话")
        voice_id = uuid.uuid4().hex
        directory = self.root / voice_id
        directory.mkdir(parents=True)
        try:
            audio_path = directory / "reference.wav"
            _convert_reference_audio(source_path, audio_path)
            duration_ms = _wav_duration_ms(audio_path)
            if not CLONE_MIN_SECONDS * 1000 <= duration_ms <= CLONE_MAX_SECONDS * 1000:
                raise CloneVoiceError(
                    f"参考音频需要 {CLONE_MIN_SECONDS}～{CLONE_MAX_SECONDS} 秒，"
                    f"当前约 {duration_ms / 1000:.1f} 秒"
                )
            meta = {
                "name": name,
                "ref_text": ref_text,
                "duration_ms": duration_ms,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (directory / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except BaseException:
            shutil.rmtree(directory)
            raise
        return self._load(directory)

    def delete(self, voice_id: str) -> None:
        voice = self.get(voice_id)
        if voice is None:
            raise CloneVoiceError("找不到该克隆音色")
        shutil.rmtree(self.root / voice.id)

    @staticmethod
    def _load(directory: Path) -> ClonedVoice:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        return ClonedVoice(
            id=directory.name,
            name=meta["name"],
            ref_text=meta["ref_text"],
            duration_ms=meta["duration_ms"],
            created_at=meta["created_at"],
            audio_path=directory / "reference.wav",
        )


def _convert_reference_audio(source_path: Path, output_path: Path) -> None:
    """统一转成 24kHz 单声道 16bit WAV，与 Qwen3-TTS 的采样率一致，也兼容用户上传的 mp3/m4a 等格式。"""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(source_path),
            "-ac", "1", "-ar", str(CLONE_SAMPLE_RATE), "-sample_fmt", "s16",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CloneVoiceError(f"无法识别参考音频：{result.stderr.strip()[-300:]}")


def _wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        return round(audio.getnframes() * 1000 / audio.getframerate())


TTS_PROVIDERS = ("mimo", "mlx_audio")


def provider_voices(provider: str, clone_store: CloneVoiceStore) -> list[VoiceOption]:
    # 克隆音色是共享音色库：MiMo 和本地模型都能用同一段参考音频复刻
    clones = [voice.option(provider) for voice in clone_store.list()]
    if provider == "mimo":
        return [*MIMO_VOICES, *clones]
    if provider == "mlx_audio":
        return clones
    return []


def find_voice(provider: str, voice_id: str, clone_store: CloneVoiceStore) -> VoiceOption | None:
    return next(
        (voice for voice in provider_voices(provider, clone_store) if voice.id == voice_id), None
    )


def voice_payload(voice: VoiceOption) -> dict[str, str | bool]:
    return asdict(voice)

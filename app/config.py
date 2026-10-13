import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_password: str
    data_dir: Path = PROJECT_DIR / "data"
    audiobooks_dir: Path = PROJECT_DIR / "audiobooks"

    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "苏打"
    mimo_tts_clone_model: str = "mimo-v2.5-tts-voiceclone"
    mimo_tts_design_model: str = "mimo-v2.5-tts-voicedesign"
    mimo_tts_rpm: int = Field(default=60, ge=1, le=100)
    mimo_tts_rate_limit_wait_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)

    tts_default_provider: str = "mlx_audio"
    mlx_audio_base_url: str = "http://127.0.0.1:8100"
    mlx_audio_tts_model: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
    mlx_audio_language: str = "chinese"
    mlx_audio_max_tokens: int = 4096
    mlx_audio_chunk_target_chars: int = 300
    mlx_audio_chunk_max_chars: int = 450

    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek/deepseek-v4.1-flash"
    llm_max_completion_tokens: int = 16384
    llm_reasoning_effort: str | None = None
    llm_max_attempts: int = 3
    openrouter_http_referer: str = "http://127.0.0.1:8000"
    openrouter_title: str = "Book TTS"
    openrouter_allow_fallbacks: bool = True
    # 逗号分隔；留空则由 OpenRouter 默认路由
    openrouter_provider_order: str = ""
    openrouter_quantizations: str = ""

    tts_concurrency: int = 3
    tts_max_attempts: int = 3
    worker_lease_seconds: int = 900
    poll_interval_seconds: int = 2

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "book-tts.sqlite3"

    @property
    def clone_voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def session_secret_path(self) -> Path:
        return self.data_dir / ".session-secret"

    def get_session_secret(self) -> str:
        """自动创建并持久化会话签名密钥，避免用户额外配置环境变量。"""
        path = self.session_secret_path
        try:
            secret = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            secret = secrets.token_urlsafe(48)
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(secret + "\n")
                path.chmod(0o600)
            except FileExistsError:
                secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            raise RuntimeError(f"会话密钥文件为空：{path}")
        return secret


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.data_dir.is_absolute():
        settings.data_dir = PROJECT_DIR / settings.data_dir
    if not settings.audiobooks_dir.is_absolute():
        settings.audiobooks_dir = PROJECT_DIR / settings.audiobooks_dir
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audiobooks_dir.mkdir(parents=True, exist_ok=True)
    return settings

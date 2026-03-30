from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    DATABASE_URL: str = "sqlite:///./docai.db"
    STORAGE_DIR: str = "./storage"

    # Embeddings / Vector
    EMBEDDING_DIM: int = 384  # sentence-transformers/all-MiniLM-L6-v2 (384-dim, Microsoft)
    EMBED_PROVIDER: str = "fastembed"  # fastembed|stub

    # Local LLM
    LLM_PROVIDER: str = "ollama"  # ollama|stub
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_CHAT_MODEL: str = "llama3.1:8b"
    OLLAMA_TEMPERATURE: float = 0.2

    # PDF export
    LIBREOFFICE_BIN: str = "soffice"

    # Auto bootstrap
    AUTO_BOOTSTRAP: bool = True
    DATA_DIR: str = "./data"
    RULEBOOK_FILENAME: str = "rulebook.pdf"

    # Safety guard:
    # When false (default), para ops must resolve through explicit section_id.
    # This prevents silent edits on the wrong section when ids are ambiguous.
    PATCHOPS_ALLOW_FIRST_NUMBERED_FALLBACK: bool = False

    # Command API and debug routes are opt-in by default.
    ENABLE_COMMAND_API: bool = False
    ENABLE_DEBUG_ROUTES: bool = False

    # Command intent extraction:
    # In sandbox we allow fast fallback when local Ollama is unavailable.
    COMMAND_INTENT_USE_LLM: bool = False
    COMMAND_INTENT_HEALTH_TIMEOUT_S: float = 1.0

    # Command content transform:
    # v1 supports deterministic stub mode plus optional LLM-backed paragraph transforms.
    COMMAND_TRANSFORM_USE_LLM: bool = False
    COMMAND_TRANSFORM_HEALTH_TIMEOUT_S: float = 1.0

    # Auth
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Doc-Engine microservice integration
    DOCENGINE_ENABLED: bool = False
    DOCENGINE_URL: str = "http://localhost:8001"
    DOCENGINE_TIMEOUT_S: float = 30.0

    # Voice/STT
    ENABLE_VOICE_INPUT: bool = False
    STT_PROVIDER: str = "faster_whisper"  # faster_whisper|stub
    STT_MODEL_NAME: str = "large-v3-turbo"
    STT_DEVICE: str = "cpu"  # cpu|cuda|auto
    STT_COMPUTE_TYPE: str = "int8"
    STT_FORCE_LANGUAGE: str | None = None
    STT_HEALTH_TIMEOUT_S: float = 120.0
    STT_MAX_AUDIO_BYTES: int = 10_485_760  # 10 MB
    STT_MIN_AUDIO_BYTES: int = 256
    STT_MIN_DURATION_MS: int = 200
    STT_MIN_CONFIDENCE: float = 0.25
    STT_CONFIRM_CONFIDENCE: float = 0.45

    def enforce_production_safety(self) -> None:
        if str(self.ENV).strip().lower() != "production":
            return
        flagged = {
            "ENABLE_COMMAND_API": self.ENABLE_COMMAND_API,
            "ENABLE_VOICE_INPUT": self.ENABLE_VOICE_INPUT,
            "COMMAND_INTENT_USE_LLM": self.COMMAND_INTENT_USE_LLM,
            "COMMAND_TRANSFORM_USE_LLM": self.COMMAND_TRANSFORM_USE_LLM,
            "ENABLE_DEBUG_ROUTES": self.ENABLE_DEBUG_ROUTES,
        }
        enabled = [name for name, value in flagged.items() if bool(value)]
        if enabled:
            raise RuntimeError(
                "Production safety guard blocked startup. Disable these flags: "
                + ", ".join(enabled)
            )


settings = Settings()
settings.enforce_production_safety()

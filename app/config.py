"""Application configuration, loaded from environment / .env.

All secrets and the LLM provider switch live here. Nothing else in the app
reads os.environ directly — import ``settings`` instead.
"""
from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM provider switch ------------------------------------------------
    # anthropic | openai | google : cloud models (need an API key)
    # ollama                      : local, developer-only (no key, no cost)
    # google uses Gemini and has a generous free tier (aistudio.google.com).
    llm_provider: Literal["anthropic", "openai", "google", "ollama"] = "ollama"
    # Optional per-provider model override. Empty -> per-provider default
    # (see app/llm.py DEFAULT_MODELS).
    llm_model: str = ""
    # Vision model used only for scanned/image PDFs (no extractable text). The
    # text model above can't see images, so this is a separate knob. Empty ->
    # per-provider default (see app/llm.py DEFAULT_VISION_MODELS). Cloud models
    # (Claude/GPT-4o) are multimodal, so their default is the same as the text
    # model; Ollama needs a dedicated vision model (e.g. qwen2.5vl:7b).
    llm_vision_model: str = ""

    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    # Google Gemini key — free from https://aistudio.google.com/app/apikey
    google_api_key: Optional[str] = None
    ollama_url: str = "http://localhost:11434"

    # --- Supabase (Postgres + Auth) ----------------------------------------
    # Placeholders by default so the app boots without a real project; fill in
    # real values (or set them in the Render dashboard) before going live.
    supabase_url: str = "https://YOUR-PROJECT.supabase.co"
    supabase_anon_key: str = "REPLACE_ME_ANON_KEY"
    supabase_service_key: str = "REPLACE_ME_SERVICE_ROLE_KEY"

    # --- App secrets --------------------------------------------------------
    # Fernet key used to encrypt stored email passwords at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    app_encryption_key: str = "REPLACE_ME_FERNET_KEY"

    # Max unread emails processed per "Read emails now" click.
    email_batch_cap: int = 20

    @property
    def supabase_configured(self) -> bool:
        """True once real Supabase credentials have been supplied."""
        return (
            "YOUR-PROJECT" not in self.supabase_url
            and "REPLACE_ME" not in self.supabase_service_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

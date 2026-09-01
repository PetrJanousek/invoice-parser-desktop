"""Application configuration, loaded from environment / .env.

All secrets and the LLM provider switch live here. Nothing else in the app
reads os.environ directly — import ``settings`` instead.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from platformdirs import user_data_dir
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
    llm_provider: Literal["anthropic", "openai", "google", "ollama"] = "anthropic"
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

    # Local app data (SQLite, encryption key, uploads). Override with DATA_DIR.
    data_dir: Path = Field(default_factory=lambda: Path(user_data_dir("InvoiceParser")))

    # Max unread emails processed per "Read emails now" click.
    email_batch_cap: int = 20

    def model_post_init(self, __context: object) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def apply_stored_llm_settings() -> None:
    """Overlay any LLM settings saved via the Settings screen onto ``settings``.

    ``settings`` is a single shared instance every module imports and holds a
    reference to (``from .config import settings``), so mutating its
    attributes in place — rather than replacing the object — is immediately
    visible everywhere (``extract.py``'s ``settings.llm_provider`` checks,
    ``llm.py``'s ``_build_chat_model``, etc.) with no changes to those
    modules. Call this once at process startup, and again right after a
    settings save so the change takes effect without a restart.

    ``db`` is imported lazily here (not at module level) because ``db.py``
    itself imports ``from .config import settings`` — a top-level import
    would be circular.
    """
    from . import db
    from .crypto import decrypt

    row = db.get_llm_settings("")
    if not row:
        return

    if row.get("provider"):
        settings.llm_provider = row["provider"]
    if row.get("model"):
        settings.llm_model = row["model"]
    if row.get("vision_model"):
        settings.llm_vision_model = row["vision_model"]
    if row.get("ollama_url"):
        settings.ollama_url = row["ollama_url"]

    key_map = (
        ("anthropic_api_key_encrypted", "anthropic_api_key"),
        ("openai_api_key_encrypted", "openai_api_key"),
        ("google_api_key_encrypted", "google_api_key"),
    )
    for encrypted_col, attr in key_map:
        encrypted = row.get(encrypted_col)
        if encrypted:
            setattr(settings, attr, decrypt(encrypted))

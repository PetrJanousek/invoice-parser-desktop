"""LLM provider abstraction via LangChain.

One factory returns a chat model for whichever provider ``LLM_PROVIDER`` names.
Swapping anthropic <-> openai <-> ollama is a config change, not a code change,
so the same extraction path runs locally (Ollama) and in production (cloud).
"""
from functools import lru_cache

from .config import settings

# Per-provider default model, used when LLM_MODEL is not set.
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    # 'gemini-flash-latest' tracks the current free-tier flash model; pinned
    # ids like gemini-2.0-flash have 0 free quota for newly created keys.
    "google": "gemini-flash-latest",
    "ollama": "qwen2.5:7b",
}

# Per-provider default *vision* model, used for scanned/image PDFs when
# LLM_VISION_MODEL is not set. Claude/GPT-4o/Gemini are multimodal, so their
# text model doubles as the vision model; Ollama needs a dedicated vision model.
DEFAULT_VISION_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-flash-latest",
    "ollama": "qwen2.5vl:7b",
}


def _model_name() -> str:
    return settings.llm_model or DEFAULT_MODELS[settings.llm_provider]


def _vision_model_name() -> str:
    return settings.llm_vision_model or DEFAULT_VISION_MODELS[settings.llm_provider]


def _build_chat_model(model: str, num_ctx: int | None = None):
    """Construct a temperature-0 chat model for the active provider.

    Imports are lazy so we only import the SDK for the active provider — a
    missing OpenAI key never breaks an Ollama-only dev setup. The provider
    classes (ChatAnthropic/ChatOpenAI/ChatOllama) are the same for text and
    vision; only the model name differs. ``num_ctx`` (Ollama only) widens the
    context window — a page image costs a few thousand tokens, well over
    Ollama's small default.
    """
    provider = settings.llm_provider

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model, temperature=0, api_key=settings.anthropic_api_key
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model, temperature=0, api_key=settings.openai_api_key
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model, temperature=0, google_api_key=settings.google_api_key
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs = {"num_ctx": num_ctx} if num_ctx else {}
        return ChatOllama(
            model=model, temperature=0, base_url=settings.ollama_url, **kwargs
        )

    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")


# Context window for the text model. Ollama's default (2k) truncates a dense
# statement page or a multi-invoice page, silently dropping content before the
# model ever sees it. 8k comfortably holds one page of text plus the structured
# JSON output. (Ignored by cloud providers, which size their own context.)
TEXT_NUM_CTX = 8192


@lru_cache
def get_chat_model():
    """Text chat model for the configured provider (cached)."""
    return _build_chat_model(_model_name(), num_ctx=TEXT_NUM_CTX)


# Context window for the vision model: one rendered page image costs a few
# thousand tokens, so the default (4k) is too small. 8k covers image + prompt
# + JSON output with headroom.
VISION_NUM_CTX = 8192


@lru_cache
def get_vision_chat_model():
    """Multimodal chat model used for scanned/image PDFs (cached)."""
    return _build_chat_model(_vision_model_name(), num_ctx=VISION_NUM_CTX)


def active_model_name() -> str:
    """Bare model id for the configured provider (no provider prefix).

    Used by call paths that talk to a provider SDK directly instead of going
    through LangChain, so they still honor the LLM_MODEL override.
    """
    return _model_name()


def active_model_label() -> str:
    """Human-readable 'provider/model' for logs and the UI."""
    return f"{settings.llm_provider}/{_model_name()}"


def active_vision_model_label() -> str:
    """Human-readable 'provider/vision-model' for logs and the UI."""
    return f"{settings.llm_provider}/{_vision_model_name()}"

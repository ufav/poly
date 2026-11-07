"""Settings from the environment / .env (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sa:sa@localhost:5432/sports"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    data_dir: Path = ROOT / "data"
    # AI Engine (docs/md1.md 7): web search for the news of the shortlisted matches, LLM through OpenRouter
    tavily_api_key: str = ""
    # any OpenAI-compatible chat API: Groq by default (GROQ_API_KEY), OpenRouter when OPENROUTER_API_KEY is set
    # and LLM_BASE_URL points to it
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"


@lru_cache
def settings() -> Settings:
    return Settings()

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
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat"


@lru_cache
def settings() -> Settings:
    return Settings()

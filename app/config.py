from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    database_url: str = "sqlite:///./beastboard.db"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    whatsapp_gateway_url: str | None = None
    whatsapp_gateway_token: str | None = None
    dashboard_data_cutoff_iso: str = "2026-03-18T00:00:00+00:00"

    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

settings = Settings()
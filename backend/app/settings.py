from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = Field(min_length=1)
    supabase_service_role_key: str = Field(min_length=1)
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""
    firebase_service_account_file: str = ""
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://localhost:4174,"
        "http://localhost:4175,http://127.0.0.1:5173,http://127.0.0.1:4173,"
        "http://127.0.0.1:4174,http://127.0.0.1:4175"
    )
    max_description_length: int = 5000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

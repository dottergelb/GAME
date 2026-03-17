from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # путь до sqlite файла бота
    SQLITE_PATH: str = "users.db"

    # токен бота нужен для проверки initData
    BOT_TOKEN: str = "PASTE_TELEGRAM_BOT_TOKEN_HERE"

    # для локальной разработки miniapp
    CORS_ORIGINS: str = "http://localhost:5173"
    ALLOW_DEV_AUTH: bool = True
    TELEGRAM_AUTH_MAX_AGE_SECONDS: int = 86400

    @field_validator("SQLITE_PATH", mode="before")
    @classmethod
    def make_sqlite_path_absolute(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return str(path)

    @property
    def cors_origins_list(self) -> list[str]:
        return [x.strip() for x in self.CORS_ORIGINS.split(",") if x.strip()]

settings = Settings()

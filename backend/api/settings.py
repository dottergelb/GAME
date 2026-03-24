from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # путь до sqlite файла бота
    SQLITE_PATH: str = "../bot/db.sqlite3"

    # токен бота нужен для проверки initData
    BOT_TOKEN: str = "PASTE_TELEGRAM_BOT_TOKEN_HERE"

    # для локальной разработки miniapp
    CORS_ORIGINS: str = "http://localhost:5173"

settings = Settings()

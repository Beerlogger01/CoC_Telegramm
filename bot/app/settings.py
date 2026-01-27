import os

from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_SETTINGS = {
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "BACKEND_URL": "backend_url",
    "BINDINGS_DB_PATH": "bindings_db_path",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    telegram_bot_token: str | None = None
    backend_url: str = "http://backend:8000"
    request_timeout_seconds: int = 10
    bindings_db_path: str = "/data/bindings.db"
    war_reminder_enabled: bool = True
    war_reminder_window_hours: int = 4
    war_reminder_interval_minutes: int = 15


settings = Settings()


def validate_settings() -> list[str]:
    missing: list[str] = []
    for env_name, field in REQUIRED_SETTINGS.items():
        env_value = os.getenv(env_name)
        value = getattr(settings, field)
        if env_value is None or not str(env_value).strip():
            missing.append(env_name)
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(env_name)
    return missing

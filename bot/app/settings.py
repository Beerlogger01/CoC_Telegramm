from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    backend_url: str = "http://backend:8000"
    request_timeout_seconds: int = 10
    bindings_db_path: str = "/data/bindings.db"
    war_reminder_enabled: bool = True
    war_reminder_window_hours: int = 4
    war_reminder_interval_minutes: int = 15


settings = Settings()

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    backend_url: str = "http://backend:8000"
    request_timeout_seconds: int = 10


settings = Settings()

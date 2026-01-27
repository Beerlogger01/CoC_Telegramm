import os

from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_SETTINGS = {
    "COC_TOKEN": "coc_token",
    "COC_CLAN_TAG": "coc_clan_tag",
    "REDIS_URL": "redis_url",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    coc_token: str | None = None
    coc_clan_tag: str | None = None
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_seconds: int = 300
    request_timeout_seconds: int = 10

    coc_api_base: str = "https://api.clashofclans.com/v1"


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

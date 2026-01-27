from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    coc_token: str
    coc_clan_tag: str
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_seconds: int = 300
    request_timeout_seconds: int = 10

    coc_api_base: str = "https://api.clashofclans.com/v1"


settings = Settings()

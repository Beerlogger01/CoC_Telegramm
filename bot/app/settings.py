import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_SETTINGS = {
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "BACKEND_URL": "backend_url",
    "BINDINGS_DB_PATH": "bindings_db_path",
}
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN"}
LOGGED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "BACKEND_URL",
    "BINDINGS_DB_PATH",
    "CLAN_GROUP_ID",
    "INVITE_TTL_MINUTES",
    "ENFORCE_CLAN_MEMBERSHIP",
    "COC_CLAN_TAG",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    telegram_bot_token: str | None = None
    backend_url: str = "http://backend:8000"
    request_timeout_seconds: int = 10
    bindings_db_path: str = "/data/bindings.db"
    clan_group_id: int | None = None
    invite_ttl_minutes: int = 10
    enforce_clan_membership: bool = False
    coc_clan_tag: str | None = None
    war_reminder_enabled: bool = True
    war_reminder_window_hours: int = 4
    war_reminder_interval_minutes: int = 15

    @field_validator("clan_group_id", mode="before")
    @classmethod
    def parse_clan_group_id(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


settings = Settings()


def mask_value(value: str | None) -> str:
    if value is None:
        return "<unset>"
    text = str(value)
    if not text.strip():
        return "<empty>"
    if len(text) <= 10:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def describe_value(value: str | None, *, sensitive: bool = False) -> str:
    if sensitive:
        return mask_value(value)
    if value is None:
        return "<unset>"
    text = str(value)
    if not text.strip():
        return "<empty>"
    return text


def env_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key in LOGGED_ENV_VARS:
        raw = os.getenv(key)
        snapshot[key] = describe_value(raw, sensitive=key in SENSITIVE_ENV_VARS)
    return snapshot


def settings_snapshot() -> dict[str, str]:
    return {
        "telegram_bot_token": mask_value(settings.telegram_bot_token),
        "backend_url": describe_value(settings.backend_url),
        "bindings_db_path": describe_value(settings.bindings_db_path),
        "clan_group_id": describe_value(
            str(settings.clan_group_id) if settings.clan_group_id is not None else None
        ),
        "invite_ttl_minutes": describe_value(str(settings.invite_ttl_minutes)),
        "enforce_clan_membership": describe_value(str(settings.enforce_clan_membership)),
        "coc_clan_tag": describe_value(settings.coc_clan_tag),
        "war_reminder_enabled": describe_value(str(settings.war_reminder_enabled)),
    }


def validate_settings() -> list[str]:
    errors: list[str] = []
    for env_name, field in REQUIRED_SETTINGS.items():
        env_value = os.getenv(env_name)
        value = getattr(settings, field)
        if env_value is None or not str(env_value).strip():
            errors.append(
                f"{env_name} is required (raw={describe_value(env_value, sensitive=env_name in SENSITIVE_ENV_VARS)})"
            )
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(
                f"{env_name} is invalid (raw={describe_value(env_value, sensitive=env_name in SENSITIVE_ENV_VARS)}, parsed={value})"
            )
    if settings.enforce_clan_membership:
        env_value = os.getenv("COC_CLAN_TAG")
        if env_value is None or not str(env_value).strip():
            errors.append(
                f"COC_CLAN_TAG is required when ENFORCE_CLAN_MEMBERSHIP=true (raw={describe_value(env_value)})"
            )
    raw_group_id = os.getenv("CLAN_GROUP_ID")
    if raw_group_id is not None and str(raw_group_id).strip() and settings.clan_group_id is None:
        errors.append(
            f"CLAN_GROUP_ID must be an integer (raw={describe_value(raw_group_id)})"
        )
    return errors

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_user_ids: list[int] = Field(
        validation_alias=AliasChoices("TELEGRAM_USER_IDS", "TELEGRAM_USER_ID")
    )
    gotsms_api_token: str
    gotsms_base_url: str = "https://app.gotsms.org"
    db_path: str = "data/skupgotsms.sqlite"
    default_autobuy_interval_min: int = 5

    @field_validator("telegram_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v


settings = Settings()

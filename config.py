from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_user_id_raw: str = Field(
        validation_alias=AliasChoices("TELEGRAM_USER_IDS", "TELEGRAM_USER_ID")
    )
    gotsms_api_token: str
    gotsms_base_url: str = "https://app.gotsms.org"
    db_path: str = "data/skupgotsms.sqlite"
    default_autobuy_interval_sec: int = 30

    # ЛК-сессия для bulk-покупки через Livewire (gotsms_lk). Опционально:
    # если заданы — автобай покупает пачкой по 25 (быстро, без лимита 30/мин),
    # иначе fallback на публичный API по 1.
    lk_session: str = ""           # cookie gotsms_session
    lk_xsrf: str = ""              # cookie XSRF-TOKEN
    lk_user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

    @computed_field  # type: ignore[misc]
    @property
    def telegram_user_ids(self) -> list[int]:
        return [int(x.strip()) for x in self.telegram_user_id_raw.split(",") if x.strip()]


settings = Settings()

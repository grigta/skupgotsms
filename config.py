from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_user_id: int
    gotsms_api_token: str
    gotsms_base_url: str = "https://app.gotsms.org"
    db_path: str = "data/skupgotsms.sqlite"
    default_autobuy_interval_min: int = 5


settings = Settings()

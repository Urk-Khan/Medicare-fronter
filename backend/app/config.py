"""
Environment configuration (secrets + deployment values), read from backend/.env.

Anything a non-developer should be able to change while the system is running
(calling hours, retries, company name, closer ring time, ...) is NOT here — it
lives in the database and is edited on the Settings page. See app/core/runtime.py.
"""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(ROOT_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- App ----
    environment: str = "development"  # development | production
    app_secret_key: str = Field("CHANGE_ME", validation_alias=AliasChoices("APP_SECRET_KEY", "SECRET_KEY"))
    access_token_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = ""
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    # ---- Supabase ----
    supabase_url: str = ""
    supabase_service_role_key: str = Field(
        "", validation_alias=AliasChoices("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    )

    # ---- Telnyx ----
    telnyx_api_key: str = ""
    telnyx_connection_id: str = ""  # Call Control Application ID
    telnyx_from_number: str = ""  # E.164 caller ID used for outbound calls
    telnyx_webhook_public_key: str = ""  # Portal -> Account -> Public Key (for signature checks)

    # Public HTTPS URL of this backend. start.py fills it in automatically from the
    # Cloudflare tunnel; set it yourself when deploying to a real server.
    public_base_url: str = "http://localhost:8000"

    # ---- Voice pipeline (same providers as the Ashad agent) ----
    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    llm_provider: str = "openai"  # openai | anthropic
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # ---- Google Sheets (optional) ----
    # Path to a service-account JSON file, or the JSON itself.
    google_service_account_json: str = ""

    @property
    def public_ws_base_url(self) -> str:
        url = self.public_base_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

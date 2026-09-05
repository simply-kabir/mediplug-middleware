from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    redis_url: str = "redis://localhost:6379/0"
    stream_key: str = "mediplug:cases"
    consumer_group: str = "mediplug-workers"
    dlq_stream_key: str = "mediplug:cases:dlq"
    max_delivery_attempts: int = 3
    claim_stale_ms: int = 60_000

    database_url: str = "postgresql://postgres:mediplug@localhost:5432/mediplug"
    supabase_url: str | None = None
    supabase_service_key: str | None = None

    embedding_backend: str = "local"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_api_key: str | None = None
    confidence_auto_accept: float = 0.82
    confidence_floor: float = 0.45

    dispatch_mode: str = "mock"
    mock_payer_url: str = "http://localhost:8081"

    nhcx_base_url: str | None = None
    nhcx_participant_code: str | None = None
    nhcx_username: str | None = None
    nhcx_password: str | None = None
    nhcx_recipient_code: str | None = None
    nhcx_private_key_path: str | None = None
    nhcx_protocol_version: str = "v0.7"


settings = Settings()

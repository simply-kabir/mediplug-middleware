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
    # Above worst-case _handle wall time (Phase 9.2): a slow MockPayerDispatcher
    # is 3 attempts x timeout=30 + backoff ~= 93s. Kept strictly higher so the
    # reclaimer never races a job that is still legitimately in flight.
    claim_stale_ms: int = 180_000
    # Bound the stream so XLEN can't grow forever (ACK removes from the PEL,
    # not the stream). Applied on enqueue + retry re-add, never on the DLQ.
    stream_maxlen: int = 10_000
    # How often reclaim_stalled sweeps for messages from dead consumers.
    # Promoted from a hardcoded 15 so the failure-drill harness can turn it down.
    reclaim_interval_s: int = 15

    database_url: str = "postgresql://postgres:mediplug@localhost:5432/mediplug"
    supabase_url: str | None = None
    supabase_service_key: str | None = None

    # --- Teammate's HMS Supabase (separate project, read-only) ---
    hms_supabase_url: str = ""
    hms_supabase_key: str = ""

    embedding_backend: str = "local"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_api_key: str | None = None
    confidence_auto_accept: float = 0.82
    confidence_floor: float = 0.45

    dispatch_mode: str = "mock"
    mock_payer_url: str = "http://localhost:8081"

    # Shared-secret gate for /admin/* on the gateway. The app has no auth and
    # CORS allow_origins=["*"], so a mutating requeue endpoint must not be
    # reachable unauthenticated. Sent as the `X-Admin-Token` header.
    admin_token: str = "dev-admin-token"

    nhcx_base_url: str | None = None
    nhcx_participant_code: str | None = None
    nhcx_username: str | None = None
    nhcx_password: str | None = None
    nhcx_recipient_code: str | None = None
    nhcx_private_key_path: str | None = None
    nhcx_protocol_version: str = "v0.7"


settings = Settings()

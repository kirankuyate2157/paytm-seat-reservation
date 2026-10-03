from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+asyncpg://seats:seats@localhost:5432/seats"
    jwt_secret: str = "dev-secret-change-me-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    admin_email: str = "admin@example.com"
    admin_password: str = "admin12345"
    # Pool sizing: pool_size + max_overflow must stay under Postgres max_connections / replicas.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: int = 60
    # Max concurrent DB transactions per process; extra requests wait (no error) instead of failing.
    db_concurrency: int = 40
    default_per_user_limit: int = 4
    max_seats_per_show: int = 50000
    max_seats_per_request: int = 10
    log_level: str = "INFO"
    seed_demo: bool = True
    cookie_secure: bool = False


settings = Settings()

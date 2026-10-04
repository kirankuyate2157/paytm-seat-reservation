from pydantic import field_validator, model_validator
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

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        """Hosts (Render, Railway, Heroku) hand out postgres:// or postgresql:// URLs; we need the asyncpg driver."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        if not v.startswith("postgresql+asyncpg://"):
            scheme = v.split("://", 1)[0] if "://" in v else "(no scheme)"
            raise ValueError(
                f"DATABASE_URL must be a PostgreSQL URL starting with postgresql:// or postgres:// (got scheme '{scheme}'). "
                "On Render use the Internal Database URL from the Postgres service's Connections tab."
            )
        return v

    @model_validator(mode="after")
    def _refuse_weak_secrets_in_prod(self):
        """Fail fast at startup if a production-like environment still uses development defaults."""
        if self.app_env.lower() in ("prod", "production", "staging"):
            problems = []
            if self.jwt_secret.startswith("dev-secret") or len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET must be set to a random value of at least 32 characters")
            if self.admin_password == "admin12345" or len(self.admin_password) < 8:
                problems.append("ADMIN_PASSWORD must be set (at least 8 characters, not the default)")
            if problems:
                raise ValueError("; ".join(problems))
        return self


settings = Settings()
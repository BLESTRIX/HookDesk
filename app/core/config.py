from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://hookdesk:hookdesk@localhost:5432/hookdesk"
    redis_url: str = "redis://localhost:6379/0"
    encryption_key: str
    public_base_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    session_hours: int = 12
    signature_tolerance_seconds: int = 300
    max_body_bytes: int = 262144
    secret_grace_seconds: int = 86400
    max_attempts: int = 5
    retry_base_seconds: float = 5
    retry_max_seconds: float = 300
    lease_seconds: int = 60
    discovery_seconds: float = 5
    demo_enabled: bool = False
    login_limit: int = 10
    login_window_seconds: int = 900

    @model_validator(mode="after")
    def validate_settings(self):
        Fernet(self.encryption_key.encode())
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL with psycopg is required")
        if self.environment == "production" and (self.demo_enabled or not self.cookie_secure):
            raise ValueError("Production requires secure cookies and disabled demo controls")
        if min(self.max_attempts, self.lease_seconds, self.max_body_bytes) < 1:
            raise ValueError("Attempt, lease, and body limits must be positive")
        if not 0 < self.retry_base_seconds <= self.retry_max_seconds:
            raise ValueError("Invalid retry bounds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

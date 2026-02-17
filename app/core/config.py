import os
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    """Configuration for the Sage Platform API service."""

    # AWS Configuration
    AWS_REGION: str = Field(default="ap-south-1")
    PROJECT_NAME: str = "Sage Platform API"
    VERSION: str = "0.1.0"
    BACKEND_CORS_ORIGINS: list[str] = ["*"]

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv(
        "LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    ENABLE_OTEL_TRACING: str = Field(default="false")
    SIGNOZ_INGESTION_KEY: Optional[str] = Field(default=None)
    SIGNOZ_SERVICE_NAME: str = Field(default="feed-journey-engine")
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(
        default="https://ingest.in.signoz.cloud:443/v1"
    )
    OTEL_SERVICE_ENVIRONMENT: str = Field(default="production")

    # Database Access gRPC Service
    DATABASE_ACCESS_GRPC_HOST: str = Field(default="localhost")
    DATABASE_ACCESS_GRPC_PORT: int = Field(default=50051)

    BASE_URL: str = Field(default="https://app.sagepilot.ai")

    # Redis config (Session management & caching)
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: str = Field(default="")
    REDIS_DB: int = Field(default=0)
    REDIS_MAX_CONNECTIONS: int = Field(default=50)
    REDIS_SOCKET_TIMEOUT: int = Field(default=5)
    REDIS_SOCKET_CONNECT_TIMEOUT: int = Field(default=5)
    REDIS_CLUSTER_MODE: bool = Field(default=True)

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()

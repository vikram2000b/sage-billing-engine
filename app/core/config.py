import os
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Configuration for the Sage Billing Engine service."""

    # Service metadata
    PROJECT_NAME: str = "Sage Billing Engine"
    VERSION: str = "0.1.0"
    BACKEND_CORS_ORIGINS: list[str] = ["*"]

    # AWS Configuration
    AWS_REGION: str = Field(default="ap-south-1")

    # Logging & Observability
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv(
        "LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    ENABLE_OTEL_TRACING: str = Field(default="false")
    SIGNOZ_INGESTION_KEY: Optional[str] = Field(default=None)
    SIGNOZ_SERVICE_NAME: str = Field(default="sage-billing-engine")
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(
        default="https://ingest.in.signoz.cloud:443/v1"
    )
    OTEL_SERVICE_ENVIRONMENT: str = Field(default="production")

    # Database Access gRPC Service
    DATABASE_ACCESS_GRPC_HOST: str = Field(default="localhost")
    DATABASE_ACCESS_GRPC_PORT: int = Field(default=50051)

    BASE_URL: str = Field(default="https://app.sagepilot.ai")

    # Redis config (entitlement cache & usage counters)
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: str = Field(default="")
    REDIS_DB: int = Field(default=0)
    REDIS_MAX_CONNECTIONS: int = Field(default=50)
    REDIS_SOCKET_TIMEOUT: int = Field(default=5)
    REDIS_SOCKET_CONNECT_TIMEOUT: int = Field(default=5)
    REDIS_CLUSTER_MODE: bool = Field(default=True)

    # Stripe Configuration
    STRIPE_SECRET_KEY: str = Field(default="")
    STRIPE_WEBHOOK_SECRET: str = Field(default="")
    STRIPE_METER_AI_CREDITS: str = Field(default="ai_credits")
    STRIPE_METER_WHATSAPP_MESSAGES: str = Field(default="whatsapp_messages")
    STRIPE_METER_EMAIL_SENDS: str = Field(default="email_sends")

    # Razorpay Configuration (for Indian payment rails)
    RAZORPAY_KEY_ID: str = Field(default="")
    RAZORPAY_KEY_SECRET: str = Field(default="")

    # SQS Queue URLs
    SQS_USAGE_EVENTS_QUEUE_URL: str = Field(default="")
    SQS_STRIPE_EVENTS_QUEUE_URL: str = Field(default="")
    SQS_PAYMENT_EVENTS_QUEUE_URL: str = Field(default="")

    # Entitlement cache TTLs (seconds)
    ENTITLEMENT_CACHE_TTL: int = Field(default=120)
    USAGE_CACHE_TTL: int = Field(default=300)

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

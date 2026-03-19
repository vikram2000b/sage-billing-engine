"""Main entry point for sage-billing-engine."""

from app.core.config import settings
from app.core.logging import logger


def main() -> None:
    """Main entry point."""
    logger.info(
        "Starting sage-billing-engine",
        extra={
            "grpc_host": settings.BILLING_GRPC_HOST,
            "grpc_port": settings.BILLING_GRPC_PORT,
            "log_level": settings.LOG_LEVEL,
        },
    )

    from app.grpc.server import run_server

    run_server()


if __name__ == "__main__":
    main()

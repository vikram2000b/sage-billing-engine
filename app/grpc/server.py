"""gRPC server setup and lifecycle management."""

from __future__ import annotations

import asyncio
from concurrent import futures
from contextlib import suppress

import grpc

from app.clients.sqs_client import sqs_client
from app.consumers.payment_events import handle_payment_event
from app.consumers.stripe_events import handle_stripe_event
from app.consumers.usage_events import handle_usage_event
from app.core.config import settings
from app.core.db import Database
from app.core.logging import logger
from app.core.redis import redis_client
from app.grpc.billing_servicer import BillingServicer
from sagepilot.billing import billing_pb2_grpc

SERVICE_NAMES = ("sagepilot.billing.BillingService",)


async def create_server() -> grpc.aio.Server:
    """Create and configure the gRPC server."""
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
            ("grpc.keepalive_time_ms", 10000),
            ("grpc.keepalive_timeout_ms", 5000),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.min_time_between_pings_ms", 10000),
            ("grpc.http2.max_pings_without_data", 0),
        ],
    )
    billing_pb2_grpc.add_BillingServiceServicer_to_server(BillingServicer(), server)

    address = f"{settings.BILLING_GRPC_HOST}:{settings.BILLING_GRPC_PORT}"
    server.add_insecure_port(address)
    logger.info(
        "Billing gRPC server configured",
        extra={
            "address": address,
            "services": list(SERVICE_NAMES),
        },
    )
    return server


async def _start_dependencies() -> None:
    """Initialize service dependencies."""
    await redis_client.connect()
    logger.info("Redis connected")

    await Database.connect()
    logger.info("Database gRPC client initialized")


def _start_consumers() -> list[asyncio.Task]:
    """Start configured SQS consumers."""
    consumers: list[asyncio.Task] = []

    if settings.SQS_USAGE_EVENTS_QUEUE_URL:
        consumers.append(
            asyncio.create_task(
                sqs_client.consume_loop(
                    queue_url=settings.SQS_USAGE_EVENTS_QUEUE_URL,
                    handler=handle_usage_event,
                ),
                name="sqs-usage-events",
            )
        )
        logger.info("Started SQS consumer: usage-events")

    if settings.SQS_STRIPE_EVENTS_QUEUE_URL:
        consumers.append(
            asyncio.create_task(
                sqs_client.consume_loop(
                    queue_url=settings.SQS_STRIPE_EVENTS_QUEUE_URL,
                    handler=handle_stripe_event,
                ),
                name="sqs-stripe-events",
            )
        )
        logger.info("Started SQS consumer: stripe-events")

    if settings.SQS_PAYMENT_EVENTS_QUEUE_URL:
        consumers.append(
            asyncio.create_task(
                sqs_client.consume_loop(
                    queue_url=settings.SQS_PAYMENT_EVENTS_QUEUE_URL,
                    handler=handle_payment_event,
                ),
                name="sqs-payment-events",
            )
        )
        logger.info("Started SQS consumer: payment-events")

    return consumers


async def _stop_consumers(consumers: list[asyncio.Task]) -> None:
    """Cancel and await all running consumer tasks."""
    for task in consumers:
        task.cancel()

    for task in consumers:
        with suppress(asyncio.CancelledError):
            await task


async def serve() -> None:
    """Start the gRPC server and run until interrupted."""
    consumers: list[asyncio.Task] = []
    server: grpc.aio.Server | None = None

    try:
        await _start_dependencies()
        consumers = _start_consumers()

        server = await create_server()
        await server.start()

        address = f"{settings.BILLING_GRPC_HOST}:{settings.BILLING_GRPC_PORT}"
        logger.info(
            "sage-billing-engine gRPC server started",
            extra={
                "address": address,
                "consumer_count": len(consumers),
            },
        )
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received")
    finally:
        logger.info("Shutting down billing-engine")

        if server is not None:
            await server.stop(grace=5)

        await _stop_consumers(consumers)

        if redis_client.is_connected:
            await redis_client.disconnect()

        await Database.close()
        logger.info("Server stopped gracefully")


def run_server() -> None:
    """Entry point for running the server."""
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("Server shutdown complete")

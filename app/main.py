"""Sage Billing Engine — FastAPI application + SQS consumer startup.

Entrypoint for the billing service. Runs:
  1. FastAPI server (entitlements, subscriptions, usage, checkout, webhooks)
  2. SQS consumers (usage events, Stripe events, payment events) as background tasks
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import logger
from app.core.redis import redis_client
from app.core.db import Database
from app.api import api_router

# SQS consumer handlers
from app.clients.sqs_client import sqs_client
from app.consumers.usage_events import handle_usage_event
from app.consumers.stripe_events import handle_stripe_event
from app.consumers.payment_events import handle_payment_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # ── Startup ──
    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")

    # Connect to Redis
    await redis_client.connect()

    # Connect to Database (gRPC)
    await Database.connect()

    # Start SQS consumers as background tasks
    consumer_tasks = []
    if settings.SQS_USAGE_EVENTS_QUEUE_URL:
        task = asyncio.create_task(
            sqs_client.consume_loop(
                queue_url=settings.SQS_USAGE_EVENTS_QUEUE_URL,
                handler=handle_usage_event,
            ),
            name="sqs-usage-events",
        )
        consumer_tasks.append(task)
        logger.info("Started SQS consumer: usage-events")

    if settings.SQS_STRIPE_EVENTS_QUEUE_URL:
        task = asyncio.create_task(
            sqs_client.consume_loop(
                queue_url=settings.SQS_STRIPE_EVENTS_QUEUE_URL,
                handler=handle_stripe_event,
            ),
            name="sqs-stripe-events",
        )
        consumer_tasks.append(task)
        logger.info("Started SQS consumer: stripe-events")

    if settings.SQS_PAYMENT_EVENTS_QUEUE_URL:
        task = asyncio.create_task(
            sqs_client.consume_loop(
                queue_url=settings.SQS_PAYMENT_EVENTS_QUEUE_URL,
                handler=handle_payment_event,
            ),
            name="sqs-payment-events",
        )
        consumer_tasks.append(task)
        logger.info("Started SQS consumer: payment-events")

    logger.info(f"{settings.PROJECT_NAME} started with {len(consumer_tasks)} SQS consumers")

    yield

    # ── Shutdown ──
    logger.info(f"Shutting down {settings.PROJECT_NAME}")

    # Cancel SQS consumer tasks
    for task in consumer_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Disconnect from services
    await redis_client.disconnect()
    await Database.close()

    logger.info(f"{settings.PROJECT_NAME} shutdown complete")


# ── FastAPI App ──

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(api_router)


# ── Health check ──

@app.get("/health")
async def health_check():
    """Service health check."""
    redis_ok = await redis_client.health_check() if redis_client.is_connected else False

    return {
        "status": "ok" if redis_ok else "degraded",
        "version": settings.VERSION,
        "service": settings.PROJECT_NAME,
        "redis": redis_ok,
    }


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
    }

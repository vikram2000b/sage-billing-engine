"""SQS consumer for Stripe partner/EventBridge events."""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import logger, tracer
from app.services.billing_projection_service import process_stripe_event


async def handle_stripe_event(event: dict[str, Any]) -> None:
    with tracer.start_as_current_span("consumer.stripe_event"):
        logger.info(
            "Received Stripe SQS event body=%s",
            json.dumps(event, default=str),
        )
        await process_stripe_event(event)

"""SQS consumer for usage events.

Consumes events from the billing-usage-events queue and:
  1. Pushes meter events to Stripe
  2. Updates real-time Redis counters
  3. (Future) Writes to Redshift for analytics
"""

from typing import Any

from app.core.logging import logger, tracer
from app.core.config import settings
from app.clients.stripe_client import stripe_client
from app.services.entitlement_service import increment_usage_counter
from app.models.enums import UsageEventType

# Map our event types to Stripe meter event names
EVENT_TYPE_TO_METER: dict[str, str] = {
    UsageEventType.AI_CREDITS.value: settings.STRIPE_METER_AI_CREDITS,
    UsageEventType.WHATSAPP_MESSAGE.value: settings.STRIPE_METER_WHATSAPP_MESSAGES,
    UsageEventType.EMAIL_SEND.value: settings.STRIPE_METER_EMAIL_SENDS,
}


async def handle_usage_event(event: dict[str, Any]) -> None:
    """Process a single usage event from SQS.

    Message format:
    {
        "event_type": "ai_credits",
        "workspace_id": "...",
        "value": 2.5,
        "idempotency_key": "msg_abc123",
        "metadata": { ... },
        "timestamp": "2026-02-17T10:30:00Z"
    }
    """
    with tracer.start_as_current_span("consumer.usage_event", attributes={
        "event_type": event.get("event_type"),
        "workspace_id": event.get("workspace_id"),
    }):
        workspace_id = event.get("workspace_id")
        event_type = event.get("event_type")
        value = float(event.get("value", 0))

        if not workspace_id or not event_type or value <= 0:
            logger.warning(f"Invalid usage event, skipping: {event}")
            return

        logger.info(
            f"Processing usage event: {event_type}={value} for workspace {workspace_id}"
        )

        # 1. Resolve Stripe customer
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            logger.error(f"No Stripe customer for workspace {workspace_id}, cannot meter usage")
            return

        # 2. Push to Stripe Meter
        meter_name = EVENT_TYPE_TO_METER.get(event_type)
        if not meter_name:
            logger.error(f"Unknown event type: {event_type}")
            return

        try:
            await stripe_client.create_meter_event(
                event_name=meter_name,
                stripe_customer_id=customer.id,
                value=value,
            )
        except Exception as e:
            logger.error(f"Failed to push meter event to Stripe: {e}", exc_info=True)
            raise  # Re-raise so the SQS message is not deleted and retried

        # 3. Update Redis real-time counter
        try:
            new_total = await increment_usage_counter(
                workspace_id=workspace_id,
                meter=event_type,
                value=value,
            )
            logger.info(f"Usage counter updated: {event_type}={new_total} for workspace {workspace_id}")
        except Exception as e:
            # Non-fatal: Redis counter is a best-effort optimization
            logger.warning(f"Failed to update Redis counter: {e}")

        # 4. TODO: Write to Redshift for analytics
        # await redshift_client.insert_billing_event(...)

        logger.info(f"Successfully processed usage event: {event_type}={value} for workspace {workspace_id}")

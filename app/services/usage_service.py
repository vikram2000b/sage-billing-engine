"""Usage service — metering, reporting, and analytics.

Handles:
  - Recording usage events (push to Stripe Meter + increment Redis counter)
  - Querying usage summaries (from Redis counters + Stripe)
  - Publishing usage events to SQS for async processing
"""

import time
from datetime import datetime, timezone
from typing import Optional, Any

from app.core.config import settings
from app.core.logging import logger, tracer
from app.clients.stripe_client import stripe_client
from app.clients.sqs_client import sqs_client
from app.services.billing_usage_service import record_usage
from app.models.enums import UsageEventType
from app.models.schemas import (
    UsageEventRequest,
    UsageReportResponse,
    UsageSummary,
    SQSUsageEvent,
)

# Map our event types to Stripe meter event names
EVENT_TYPE_TO_METER: dict[UsageEventType, str] = {
    UsageEventType.AI_CREDITS: settings.STRIPE_METER_AI_CREDITS,
    UsageEventType.WHATSAPP_MESSAGE: settings.STRIPE_METER_WHATSAPP_MESSAGES,
    UsageEventType.EMAIL_SEND: settings.STRIPE_METER_EMAIL_SENDS,
}


async def record_usage_event(event: UsageEventRequest) -> dict[str, Any]:
    """Record a usage event through the billing-owned allocation service.

    This is the synchronous path — called directly by the API when
    the caller needs immediate confirmation. For high-throughput
    paths (AI messages, WhatsApp), prefer publishing to SQS.
    """
    with tracer.start_as_current_span("usage.record", attributes={
        "workspace_id": event.workspace_id,
        "event_type": event.event_type.value,
        "value": event.value,
    }):
        decision = await record_usage(
            {
                "version": "v1",
                "source_service": "billing-http",
                "workspace_id": event.workspace_id,
                "event_type": event.event_type.value,
                "value": event.value,
                "idempotency_key": event.idempotency_key,
                "occurred_at": event.timestamp,
                "metadata": event.metadata,
            }
        )
        return {
            "status": "recorded" if decision.allowed else "blocked",
            "mode": decision.mode,
            "prepaid_value": decision.prepaid_value,
            "overage_value": decision.overage_value,
            "meter_event_id": decision.stripe_meter_event_id or None,
        }


async def publish_usage_event(event: UsageEventRequest) -> str:
    """Publish a usage event to SQS for async processing.

    This is the preferred path for high-throughput usage events.
    The SQS consumer will call record_usage_event.
    """
    with tracer.start_as_current_span("usage.publish"):
        if not settings.SQS_USAGE_EVENTS_QUEUE_URL:
            raise RuntimeError("SQS_USAGE_EVENTS_QUEUE_URL is not configured")

        sqs_message = SQSUsageEvent(
            event_type=event.event_type,
            workspace_id=event.workspace_id,
            value=event.value,
            idempotency_key=event.idempotency_key,
            metadata=event.metadata,
            timestamp=event.timestamp or datetime.now(timezone.utc),
        )

        message_id = await sqs_client.publish(
            queue_url=settings.SQS_USAGE_EVENTS_QUEUE_URL,
            message=sqs_message.model_dump(mode="json"),
            message_group_id=event.workspace_id,
            deduplication_id=event.idempotency_key,
        )

        logger.info(f"Published usage event to SQS: {message_id}")
        return message_id


async def get_usage_report(
    workspace_id: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> UsageReportResponse:
    """Get a usage report for a workspace from Stripe meter summaries."""
    with tracer.start_as_current_span("usage.report", attributes={
        "workspace_id": workspace_id,
    }):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {workspace_id}")

        # Get the active subscription to determine the current billing period
        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            raise ValueError(f"No active subscription for workspace {workspace_id}")

        period_start = start_time or datetime.fromtimestamp(
            sub["current_period_start"], tz=timezone.utc
        )
        period_end = end_time or datetime.fromtimestamp(
            sub["current_period_end"], tz=timezone.utc
        )

        start_ts = int(period_start.timestamp())
        end_ts = int(period_end.timestamp())

        meters: dict[str, UsageSummary] = {}

        for event_type, meter_name in EVENT_TYPE_TO_METER.items():
            try:
                summary = await stripe_client.get_meter_event_summary(
                    customer_id=customer.id,
                    meter_id=meter_name,
                    start_time=start_ts,
                    end_time=end_ts,
                )
                aggregated_value = 0.0
                if summary and summary.data:
                    aggregated_value = float(summary.data[0].get("aggregated_value", 0))

                meters[event_type.value] = UsageSummary(
                    used=aggregated_value,
                )
            except Exception as e:
                logger.warning(f"Could not fetch meter summary for {meter_name}: {e}")
                meters[event_type.value] = UsageSummary(used=0.0)

        return UsageReportResponse(
            workspace_id=workspace_id,
            period_start=period_start,
            period_end=period_end,
            meters=meters,
        )

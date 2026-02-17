"""SQS consumer for Stripe webhook events.

Consumes events from the billing-stripe-events queue
(Stripe → SNS → SQS or Stripe → webhook → SQS).

Handles:
  - Subscription lifecycle (created, updated, deleted, trial ending)
  - Invoice lifecycle (paid, payment_failed, upcoming)
  - Payment intents
  - Meter usage reported
"""

from typing import Any

from app.core.logging import logger, tracer
from app.services.entitlement_service import (
    invalidate_entitlements,
    reset_usage_counter,
)
from app.models.enums import UsageEventType


async def handle_stripe_event(event: dict[str, Any]) -> None:
    """Process a single Stripe event from SQS.

    Message format:
    {
        "event_id": "evt_...",
        "event_type": "invoice.paid",
        "data": { "object": { ... } },
        "created": 1708000000
    }
    """
    event_type = event.get("event_type", "")
    event_id = event.get("event_id", "")
    data_object = event.get("data", {}).get("object", {})

    with tracer.start_as_current_span("consumer.stripe_event", attributes={
        "stripe.event_type": event_type,
        "stripe.event_id": event_id,
    }):
        logger.info(f"Processing Stripe event: {event_type} ({event_id})")

        try:
            # ─── Subscription events ───
            if event_type == "customer.subscription.created":
                await _handle_subscription_created(data_object)

            elif event_type == "customer.subscription.updated":
                await _handle_subscription_updated(data_object)

            elif event_type == "customer.subscription.deleted":
                await _handle_subscription_deleted(data_object)

            elif event_type == "customer.subscription.trial_will_end":
                await _handle_trial_ending(data_object)

            # ─── Invoice events ───
            elif event_type == "invoice.paid":
                await _handle_invoice_paid(data_object)

            elif event_type == "invoice.payment_failed":
                await _handle_invoice_payment_failed(data_object)

            elif event_type == "invoice.upcoming":
                await _handle_invoice_upcoming(data_object)

            # ─── Billing meter events ───
            elif event_type == "billing.meter.usage_reported":
                await _handle_usage_reported(data_object)

            else:
                logger.info(f"Unhandled Stripe event type: {event_type}")

        except Exception as e:
            logger.error(f"Error processing Stripe event {event_type}: {e}", exc_info=True)
            raise


# ─── Subscription handlers ───


async def _handle_subscription_created(data: dict) -> None:
    """New subscription created — invalidate entitlement cache."""
    workspace_id = _extract_workspace_id(data)
    if workspace_id:
        await invalidate_entitlements(workspace_id)
        logger.info(f"Subscription created for workspace {workspace_id}: {data.get('id')}")


async def _handle_subscription_updated(data: dict) -> None:
    """Subscription updated (plan change, status change, renewal).

    This is the most important handler — covers:
      - Plan upgrades/downgrades
      - Status changes (active → past_due, trialing → active, etc.)
      - Billing period renewals (reset usage counters)
    """
    workspace_id = _extract_workspace_id(data)
    if not workspace_id:
        return

    # Invalidate entitlement cache so next check fetches fresh data
    await invalidate_entitlements(workspace_id)

    # Check if this is a billing period renewal (new period = reset counters)
    previous_attributes = data.get("previous_attributes", {})
    if "current_period_start" in previous_attributes:
        logger.info(f"Billing period renewed for workspace {workspace_id}, resetting usage counters")
        for meter in UsageEventType:
            await reset_usage_counter(workspace_id, meter.value)

    status = data.get("status")
    logger.info(f"Subscription updated for workspace {workspace_id}: status={status}")


async def _handle_subscription_deleted(data: dict) -> None:
    """Subscription canceled/deleted — invalidate entitlements."""
    workspace_id = _extract_workspace_id(data)
    if workspace_id:
        await invalidate_entitlements(workspace_id)
        logger.info(f"Subscription deleted for workspace {workspace_id}: {data.get('id')}")


async def _handle_trial_ending(data: dict) -> None:
    """Trial ending in 3 days — send notification."""
    workspace_id = _extract_workspace_id(data)
    if workspace_id:
        logger.info(f"Trial ending soon for workspace {workspace_id}")
        # TODO: Send notification via WhatsApp/email


# ─── Invoice handlers ───


async def _handle_invoice_paid(data: dict) -> None:
    """Invoice paid — subscription is healthy, refresh entitlements.

    This fires for:
      - Auto-charged payments (Stripe)
      - Payments marked as paid_out_of_band (Razorpay, manual)
    """
    workspace_id = _extract_workspace_id_from_invoice(data)
    if workspace_id:
        await invalidate_entitlements(workspace_id)
        logger.info(
            f"Invoice paid for workspace {workspace_id}: "
            f"{data.get('id')} amount={data.get('amount_paid')} {data.get('currency')}"
        )


async def _handle_invoice_payment_failed(data: dict) -> None:
    """Invoice payment failed — subscription may go past_due.

    Invalidate cache so entitlement checks reflect the past_due status.
    """
    workspace_id = _extract_workspace_id_from_invoice(data)
    if workspace_id:
        await invalidate_entitlements(workspace_id)
        logger.warning(
            f"Payment failed for workspace {workspace_id}: "
            f"invoice={data.get('id')} attempt={data.get('attempt_count')}"
        )
        # TODO: Send payment failure notification


async def _handle_invoice_upcoming(data: dict) -> None:
    """Invoice upcoming — reminder for send_invoice customers."""
    workspace_id = _extract_workspace_id_from_invoice(data)
    if workspace_id:
        logger.info(f"Upcoming invoice for workspace {workspace_id}: {data.get('id')}")
        # TODO: Send upcoming invoice notification for manual payment customers


# ─── Meter handlers ───


async def _handle_usage_reported(data: dict) -> None:
    """Stripe has aggregated usage — can sync Redis counters if needed."""
    logger.info(f"Usage reported by Stripe: {data.get('id')}")
    # This can be used to periodically sync Redis counters with Stripe's
    # aggregated values to correct any drift.


# ─── Helpers ───


def _extract_workspace_id(data: dict) -> str | None:
    """Extract workspace_id from subscription metadata."""
    metadata = data.get("metadata", {})
    workspace_id = metadata.get("workspace_id")
    if not workspace_id:
        # Try customer metadata
        customer_metadata = data.get("customer", {})
        if isinstance(customer_metadata, dict):
            workspace_id = customer_metadata.get("metadata", {}).get("workspace_id")
    return workspace_id


def _extract_workspace_id_from_invoice(data: dict) -> str | None:
    """Extract workspace_id from invoice metadata or subscription metadata."""
    # First try invoice-level metadata
    metadata = data.get("metadata", {})
    workspace_id = metadata.get("workspace_id")
    if workspace_id:
        return workspace_id

    # Try subscription metadata
    subscription = data.get("subscription")
    if isinstance(subscription, dict):
        return subscription.get("metadata", {}).get("workspace_id")

    # Try lines → subscription metadata
    lines = data.get("lines", {}).get("data", [])
    for line in lines:
        sub_meta = line.get("metadata", {})
        if sub_meta.get("workspace_id"):
            return sub_meta["workspace_id"]

    return None

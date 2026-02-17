"""SQS consumer for external payment events (Razorpay, Zoho Books, manual reconciliation).

Handles:
  - Razorpay payment.captured → mark Stripe invoice as paid
  - Razorpay payment.failed → notify workspace
  - Manual reconciliation events
  - Zoho Books payment events
"""

from typing import Any

from app.core.logging import logger, tracer
from app.clients.stripe_client import stripe_client
from app.services.entitlement_service import invalidate_entitlements


async def handle_payment_event(event: dict[str, Any]) -> None:
    """Process a single payment event from SQS.

    Message format:
    {
        "source": "razorpay" | "manual_reconciliation" | "zoho_books",
        "event_type": "payment.captured",
        "workspace_id": "...",
        "amount": 5000,
        "currency": "INR",
        "metadata": { ... },
        "timestamp": "2026-02-17T10:30:00Z"
    }
    """
    source = event.get("source", "unknown")
    event_type = event.get("event_type", "unknown")

    with tracer.start_as_current_span("consumer.payment_event", attributes={
        "payment.source": source,
        "payment.event_type": event_type,
    }):
        logger.info(f"Processing payment event: {source}.{event_type}")

        try:
            if source == "razorpay":
                await _handle_razorpay_event(event)
            elif source == "manual_reconciliation":
                await _handle_manual_reconciliation(event)
            elif source == "zoho_books":
                await _handle_zoho_event(event)
            else:
                logger.warning(f"Unknown payment source: {source}")
        except Exception as e:
            logger.error(f"Error processing payment event: {e}", exc_info=True)
            raise


async def _handle_razorpay_event(event: dict[str, Any]) -> None:
    """Handle Razorpay payment events."""
    event_type = event.get("event_type", "")
    metadata = event.get("metadata", {})

    if event_type == "payment.captured":
        # Extract the Stripe invoice ID from Razorpay payment notes
        payment_entity = metadata.get("payload", {}).get("payment", {}).get("entity", {})
        notes = payment_entity.get("notes", {})
        stripe_invoice_id = notes.get("stripe_invoice_id")
        workspace_id = notes.get("workspace_id")
        razorpay_payment_id = payment_entity.get("id")

        if not stripe_invoice_id:
            logger.error(f"Razorpay payment {razorpay_payment_id} has no stripe_invoice_id in notes")
            return

        # Mark the Stripe invoice as paid out-of-band
        try:
            await stripe_client.mark_invoice_paid_out_of_band(stripe_invoice_id)
            logger.info(
                f"Razorpay payment {razorpay_payment_id} reconciled with "
                f"Stripe invoice {stripe_invoice_id} for workspace {workspace_id}"
            )
        except Exception as e:
            logger.error(f"Failed to mark Stripe invoice as paid: {e}", exc_info=True)
            raise

        # Invalidate entitlements
        if workspace_id:
            await invalidate_entitlements(workspace_id)

        # TODO: Write to Redshift audit trail
        # await record_payment_event(source="razorpay", ...)

    elif event_type == "payment.failed":
        workspace_id = event.get("workspace_id")
        logger.warning(f"Razorpay payment failed for workspace {workspace_id}")
        # TODO: Send payment failure notification

    else:
        logger.info(f"Unhandled Razorpay event: {event_type}")


async def _handle_manual_reconciliation(event: dict[str, Any]) -> None:
    """Handle manual bank transfer reconciliation events.

    These are published by the reconcile API endpoint.
    """
    stripe_invoice_id = event.get("stripe_invoice_id")
    workspace_id = event.get("workspace_id")
    bank_reference = event.get("bank_reference")

    if not stripe_invoice_id:
        logger.error("Manual reconciliation event missing stripe_invoice_id")
        return

    try:
        await stripe_client.mark_invoice_paid_out_of_band(stripe_invoice_id)
        logger.info(
            f"Manual payment reconciled: invoice={stripe_invoice_id} "
            f"workspace={workspace_id} ref={bank_reference}"
        )
    except Exception as e:
        logger.error(f"Failed to reconcile manual payment: {e}", exc_info=True)
        raise

    if workspace_id:
        await invalidate_entitlements(workspace_id)

    # TODO: Write to Redshift audit trail


async def _handle_zoho_event(event: dict[str, Any]) -> None:
    """Handle Zoho Books payment events."""
    event_type = event.get("event_type", "")
    logger.info(f"Zoho Books event: {event_type}")

    # TODO: Implement Zoho Books payment reconciliation
    # Similar pattern to Razorpay: extract invoice reference, mark Stripe invoice paid

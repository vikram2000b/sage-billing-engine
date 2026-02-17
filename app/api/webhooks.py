"""Webhook receiver endpoints.

These are backup/direct webhook receivers. The primary path for
Stripe events is: Stripe → SNS → SQS → consumer.
These endpoints exist for:
  1. Direct Stripe webhook (fallback if SNS/SQS is not set up)
  2. Razorpay webhook receiver
"""

from fastapi import APIRouter, Request, HTTPException, Header

from app.core.logging import logger
from app.clients.stripe_client import StripeClient
from app.clients.sqs_client import sqs_client
from app.core.config import settings

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """Receive Stripe webhook events directly.

    Verifies the webhook signature and forwards to the Stripe events
    SQS queue for processing by the consumer.
    """
    payload = await request.body()

    try:
        event = StripeClient.construct_webhook_event(payload, stripe_signature)
    except Exception as e:
        logger.error(f"Stripe webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    logger.info(f"Received Stripe webhook: {event['type']} ({event['id']})")

    # Forward to SQS for processing
    if settings.SQS_STRIPE_EVENTS_QUEUE_URL:
        await sqs_client.publish(
            queue_url=settings.SQS_STRIPE_EVENTS_QUEUE_URL,
            message={
                "event_id": event["id"],
                "event_type": event["type"],
                "data": dict(event["data"]),
                "created": event["created"],
            },
        )
    else:
        # Process inline if SQS is not configured (dev mode)
        from app.consumers.stripe_events import handle_stripe_event
        await handle_stripe_event({
            "event_id": event["id"],
            "event_type": event["type"],
            "data": dict(event["data"]),
            "created": event["created"],
        })

    return {"status": "received"}


@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    """Receive Razorpay webhook events.

    Forwards to the payment events SQS queue for processing.
    """
    payload = await request.json()

    # TODO: Verify Razorpay webhook signature
    # razorpay_client.utility.verify_webhook_signature(body, signature, secret)

    event_type = payload.get("event", "unknown")
    logger.info(f"Received Razorpay webhook: {event_type}")

    # Forward to SQS for processing
    if settings.SQS_PAYMENT_EVENTS_QUEUE_URL:
        await sqs_client.publish(
            queue_url=settings.SQS_PAYMENT_EVENTS_QUEUE_URL,
            message={
                "source": "razorpay",
                "event_type": event_type,
                "workspace_id": payload.get("payload", {}).get("payment", {}).get("entity", {}).get("notes", {}).get("workspace_id", ""),
                "amount": payload.get("payload", {}).get("payment", {}).get("entity", {}).get("amount", 0),
                "currency": payload.get("payload", {}).get("payment", {}).get("entity", {}).get("currency", "INR"),
                "metadata": payload,
            },
        )
    else:
        from app.consumers.payment_events import handle_payment_event
        await handle_payment_event({
            "source": "razorpay",
            "event_type": event_type,
            "metadata": payload,
        })

    return {"status": "received"}

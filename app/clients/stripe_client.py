"""Stripe API client wrapper for the billing engine."""

import stripe
from typing import Optional, Any

from app.core.config import settings
from app.core.logging import logger, tracer

# Initialize Stripe with our secret key
stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeClient:
    """Wrapper around the Stripe SDK for billing operations.

    All methods are thin wrappers that add logging, tracing, and
    error handling on top of the Stripe SDK.
    """

    # ─── Customers ───

    @staticmethod
    async def get_or_create_customer(
        workspace_id: str,
        email: str,
        name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> stripe.Customer:
        """Find existing Stripe customer by workspace_id or create a new one."""
        with tracer.start_as_current_span("stripe.get_or_create_customer", attributes={
            "workspace_id": workspace_id,
        }):
            # Search for existing customer by workspace_id in metadata
            existing = stripe.Customer.search(
                query=f"metadata['workspace_id']:'{workspace_id}'"
            )
            if existing.data:
                logger.info(f"Found existing Stripe customer for workspace {workspace_id}")
                return existing.data[0]

            # Create new customer
            customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata={
                    "workspace_id": workspace_id,
                    **(metadata or {}),
                },
            )
            logger.info(f"Created Stripe customer {customer.id} for workspace {workspace_id}")
            return customer

    @staticmethod
    async def get_customer_by_workspace(workspace_id: str) -> Optional[stripe.Customer]:
        """Look up a Stripe customer by workspace_id metadata."""
        with tracer.start_as_current_span("stripe.get_customer_by_workspace"):
            result = stripe.Customer.search(
                query=f"metadata['workspace_id']:'{workspace_id}'"
            )
            return result.data[0] if result.data else None

    # ─── Subscriptions ───

    @staticmethod
    async def create_subscription(
        customer_id: str,
        price_id: str,
        collection_method: str = "charge_automatically",
        trial_days: Optional[int] = None,
        metadata: Optional[dict] = None,
        preferred_gateway: str = "stripe",
    ) -> stripe.Subscription:
        """Create a new Stripe subscription."""
        with tracer.start_as_current_span("stripe.create_subscription", attributes={
            "customer_id": customer_id,
            "price_id": price_id,
        }):
            params: dict[str, Any] = {
                "customer": customer_id,
                "items": [{"price": price_id}],
                "collection_method": collection_method,
                "metadata": metadata or {},
            }

            if collection_method == "send_invoice":
                params["days_until_due"] = 30
                params["metadata"]["preferred_gateway"] = preferred_gateway

            if trial_days:
                params["trial_period_days"] = trial_days

            sub = stripe.Subscription.create(**params)
            logger.info(f"Created subscription {sub.id} for customer {customer_id}")
            return sub

    @staticmethod
    async def get_subscription(subscription_id: str) -> stripe.Subscription:
        """Retrieve a Stripe subscription."""
        with tracer.start_as_current_span("stripe.get_subscription"):
            return stripe.Subscription.retrieve(subscription_id)

    @staticmethod
    async def get_active_subscription(customer_id: str) -> Optional[stripe.Subscription]:
        """Get the active subscription for a customer."""
        with tracer.start_as_current_span("stripe.get_active_subscription"):
            subs = stripe.Subscription.list(
                customer=customer_id,
                status="active",
                limit=1,
            )
            if subs.data:
                return subs.data[0]

            # Also check trialing
            subs = stripe.Subscription.list(
                customer=customer_id,
                status="trialing",
                limit=1,
            )
            return subs.data[0] if subs.data else None

    @staticmethod
    async def cancel_subscription(
        subscription_id: str,
        cancel_immediately: bool = False,
    ) -> stripe.Subscription:
        """Cancel a subscription immediately or at period end."""
        with tracer.start_as_current_span("stripe.cancel_subscription"):
            if cancel_immediately:
                sub = stripe.Subscription.cancel(subscription_id)
            else:
                sub = stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True,
                )
            logger.info(f"Canceled subscription {subscription_id} (immediate={cancel_immediately})")
            return sub

    @staticmethod
    async def revoke_cancellation(subscription_id: str) -> stripe.Subscription:
        """Revoke a pending cancellation."""
        with tracer.start_as_current_span("stripe.revoke_cancellation"):
            sub = stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=False,
            )
            logger.info(f"Revoked cancellation for subscription {subscription_id}")
            return sub

    @staticmethod
    async def change_subscription_plan(
        subscription_id: str,
        new_price_id: str,
        proration_behavior: str = "create_prorations",
    ) -> stripe.Subscription:
        """Change the plan/price on an existing subscription."""
        with tracer.start_as_current_span("stripe.change_plan"):
            sub = stripe.Subscription.retrieve(subscription_id)
            sub = stripe.Subscription.modify(
                subscription_id,
                items=[{
                    "id": sub["items"]["data"][0]["id"],
                    "price": new_price_id,
                }],
                proration_behavior=proration_behavior,
            )
            logger.info(f"Changed subscription {subscription_id} to price {new_price_id}")
            return sub

    @staticmethod
    async def pause_subscription(subscription_id: str) -> stripe.Subscription:
        """Pause a subscription (stop invoicing)."""
        with tracer.start_as_current_span("stripe.pause_subscription"):
            sub = stripe.Subscription.modify(
                subscription_id,
                pause_collection={"behavior": "void"},
            )
            logger.info(f"Paused subscription {subscription_id}")
            return sub

    @staticmethod
    async def resume_subscription(subscription_id: str) -> stripe.Subscription:
        """Resume a paused subscription."""
        with tracer.start_as_current_span("stripe.resume_subscription"):
            sub = stripe.Subscription.modify(
                subscription_id,
                pause_collection="",  # Empty string clears the pause
            )
            logger.info(f"Resumed subscription {subscription_id}")
            return sub

    # ─── Usage Metering ───

    @staticmethod
    async def create_meter_event(
        event_name: str,
        stripe_customer_id: str,
        value: float,
        identifier: Optional[dict] = None,
        timestamp: Optional[int] = None,
    ) -> Any:
        """Push a usage meter event to Stripe."""
        with tracer.start_as_current_span("stripe.create_meter_event", attributes={
            "event_name": event_name,
            "value": value,
        }):
            params: dict[str, Any] = {
                "event_name": event_name,
                "payload": {
                    "value": str(value),
                    "stripe_customer_id": stripe_customer_id,
                },
            }
            if timestamp:
                params["timestamp"] = timestamp

            event = stripe.billing.MeterEvent.create(**params)
            logger.info(
                f"Created meter event: {event_name}={value} for customer {stripe_customer_id}"
            )
            return event

    @staticmethod
    async def get_meter_event_summary(
        customer_id: str,
        meter_id: str,
        start_time: int,
        end_time: int,
    ) -> Any:
        """Get aggregated usage for a meter in a time range."""
        with tracer.start_as_current_span("stripe.get_meter_summary"):
            summaries = stripe.billing.Meter.list_event_summaries(
                meter_id,
                customer=customer_id,
                start_time=start_time,
                end_time=end_time,
            )
            return summaries

    # ─── Invoices ───

    @staticmethod
    async def get_invoice(invoice_id: str) -> stripe.Invoice:
        """Retrieve a Stripe invoice."""
        with tracer.start_as_current_span("stripe.get_invoice"):
            return stripe.Invoice.retrieve(invoice_id)

    @staticmethod
    async def list_invoices(
        customer_id: str,
        status: Optional[str] = None,
        limit: int = 10,
    ) -> list[stripe.Invoice]:
        """List invoices for a customer."""
        with tracer.start_as_current_span("stripe.list_invoices"):
            params: dict[str, Any] = {
                "customer": customer_id,
                "limit": limit,
            }
            if status:
                params["status"] = status
            return stripe.Invoice.list(**params).data

    @staticmethod
    async def send_invoice(invoice_id: str) -> stripe.Invoice:
        """Send an invoice via Stripe (email)."""
        with tracer.start_as_current_span("stripe.send_invoice"):
            invoice = stripe.Invoice.send_invoice(invoice_id)
            logger.info(f"Sent invoice {invoice_id}")
            return invoice

    @staticmethod
    async def mark_invoice_paid_out_of_band(invoice_id: str) -> stripe.Invoice:
        """Mark an invoice as paid via external payment (Razorpay, bank transfer, etc.)."""
        with tracer.start_as_current_span("stripe.mark_paid_out_of_band"):
            invoice = stripe.Invoice.pay(invoice_id, paid_out_of_band=True)
            logger.info(f"Marked invoice {invoice_id} as paid out of band")
            return invoice

    @staticmethod
    async def void_invoice(invoice_id: str) -> stripe.Invoice:
        """Void an invoice."""
        with tracer.start_as_current_span("stripe.void_invoice"):
            invoice = stripe.Invoice.void_invoice(invoice_id)
            logger.info(f"Voided invoice {invoice_id}")
            return invoice

    @staticmethod
    async def list_overdue_invoices() -> list[stripe.Invoice]:
        """List all overdue invoices (open and past due date)."""
        import time
        with tracer.start_as_current_span("stripe.list_overdue_invoices"):
            invoices = stripe.Invoice.list(
                status="open",
                due_date={"lt": int(time.time())},
            )
            return invoices.data

    # ─── Checkout Sessions ───

    @staticmethod
    async def create_checkout_session(
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        mode: str = "subscription",
        metadata: Optional[dict] = None,
    ) -> stripe.checkout.Session:
        """Create a Stripe Checkout session."""
        with tracer.start_as_current_span("stripe.create_checkout"):
            session = stripe.checkout.Session.create(
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": 1}],
                mode=mode,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=metadata or {},
            )
            logger.info(f"Created checkout session {session.id} for customer {customer_id}")
            return session

    # ─── Products & Prices (catalog) ───

    @staticmethod
    async def list_products(active: bool = True) -> list[stripe.Product]:
        """List Stripe products (plans catalog)."""
        with tracer.start_as_current_span("stripe.list_products"):
            return stripe.Product.list(active=active).data

    @staticmethod
    async def list_prices(product_id: Optional[str] = None, active: bool = True) -> list[stripe.Price]:
        """List Stripe prices, optionally filtered by product."""
        with tracer.start_as_current_span("stripe.list_prices"):
            params: dict[str, Any] = {"active": active}
            if product_id:
                params["product"] = product_id
            return stripe.Price.list(**params).data

    # ─── Webhook Verification ───

    @staticmethod
    def construct_webhook_event(
        payload: bytes,
        sig_header: str,
    ) -> stripe.Event:
        """Verify and construct a Stripe webhook event."""
        return stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )


# Singleton instance
stripe_client = StripeClient()

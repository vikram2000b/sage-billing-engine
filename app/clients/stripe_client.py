"""Stripe API client wrapper for the billing engine."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import stripe

from app.core.config import settings
from app.core.logging import logger, tracer

stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeClient:
    """Thin async wrapper around the synchronous Stripe SDK."""

    @staticmethod
    async def _call(fn, /, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    async def get_or_create_customer(
        workspace_id: str,
        email: str,
        name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> stripe.Customer:
        with tracer.start_as_current_span(
            "stripe.get_or_create_customer",
            attributes={"workspace_id": workspace_id},
        ):
            existing = await StripeClient._call(
                stripe.Customer.search,
                query=f"metadata['workspace_id']:'{workspace_id}'",
            )
            if existing.data:
                logger.info(
                    "Found existing Stripe customer for workspace %s",
                    workspace_id,
                )
                return existing.data[0]

            customer = await StripeClient._call(
                stripe.Customer.create,
                email=email,
                name=name,
                metadata={
                    "workspace_id": workspace_id,
                    **(metadata or {}),
                },
            )
            logger.info(
                "Created Stripe customer %s for workspace %s",
                customer.id,
                workspace_id,
            )
            return customer

    @staticmethod
    async def get_customer_by_workspace(workspace_id: str) -> Optional[stripe.Customer]:
        with tracer.start_as_current_span("stripe.get_customer_by_workspace"):
            result = await StripeClient._call(
                stripe.Customer.search,
                query=f"metadata['workspace_id']:'{workspace_id}'",
            )
            return result.data[0] if result.data else None

    @staticmethod
    async def create_subscription(
        customer_id: str,
        price_id: str,
        collection_method: str = "charge_automatically",
        trial_days: Optional[int] = None,
        metadata: Optional[dict] = None,
        preferred_gateway: str = "stripe",
    ) -> stripe.Subscription:
        with tracer.start_as_current_span(
            "stripe.create_subscription",
            attributes={"customer_id": customer_id, "price_id": price_id},
        ):
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

            sub = await StripeClient._call(stripe.Subscription.create, **params)
            logger.info("Created subscription %s for customer %s", sub.id, customer_id)
            return sub

    @staticmethod
    async def get_subscription(
        subscription_id: str,
        *,
        expand: Optional[list[str]] = None,
    ) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.get_subscription"):
            params: dict[str, Any] = {}
            if expand:
                params["expand"] = expand
            return await StripeClient._call(
                stripe.Subscription.retrieve,
                subscription_id,
                **params,
            )

    @staticmethod
    async def get_active_subscription(
        customer_id: str,
    ) -> Optional[stripe.Subscription]:
        with tracer.start_as_current_span("stripe.get_active_subscription"):
            for status in ("active", "trialing"):
                subs = await StripeClient._call(
                    stripe.Subscription.list,
                    customer=customer_id,
                    status=status,
                    limit=1,
                )
                if subs.data:
                    return subs.data[0]
            return None

    @staticmethod
    async def cancel_subscription(
        subscription_id: str,
        cancel_immediately: bool = False,
    ) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.cancel_subscription"):
            if cancel_immediately:
                sub = await StripeClient._call(
                    stripe.Subscription.cancel,
                    subscription_id,
                )
            else:
                sub = await StripeClient._call(
                    stripe.Subscription.modify,
                    subscription_id,
                    cancel_at_period_end=True,
                )
            logger.info(
                "Canceled subscription %s (immediate=%s)",
                subscription_id,
                cancel_immediately,
            )
            return sub

    @staticmethod
    async def revoke_cancellation(subscription_id: str) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.revoke_cancellation"):
            sub = await StripeClient._call(
                stripe.Subscription.modify,
                subscription_id,
                cancel_at_period_end=False,
            )
            logger.info("Revoked cancellation for subscription %s", subscription_id)
            return sub

    @staticmethod
    async def change_subscription_plan(
        subscription_id: str,
        new_price_id: str,
        proration_behavior: str = "create_prorations",
    ) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.change_plan"):
            sub = await StripeClient._call(stripe.Subscription.retrieve, subscription_id)
            updated = await StripeClient._call(
                stripe.Subscription.modify,
                subscription_id,
                items=[
                    {
                        "id": sub["items"]["data"][0]["id"],
                        "price": new_price_id,
                    }
                ],
                proration_behavior=proration_behavior,
            )
            logger.info(
                "Changed subscription %s to price %s",
                subscription_id,
                new_price_id,
            )
            return updated

    @staticmethod
    async def pause_subscription(subscription_id: str) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.pause_subscription"):
            sub = await StripeClient._call(
                stripe.Subscription.modify,
                subscription_id,
                pause_collection={"behavior": "void"},
            )
            logger.info("Paused subscription %s", subscription_id)
            return sub

    @staticmethod
    async def resume_subscription(subscription_id: str) -> stripe.Subscription:
        with tracer.start_as_current_span("stripe.resume_subscription"):
            sub = await StripeClient._call(
                stripe.Subscription.modify,
                subscription_id,
                pause_collection="",
            )
            logger.info("Resumed subscription %s", subscription_id)
            return sub

    @staticmethod
    async def create_meter_event(
        event_name: str,
        stripe_customer_id: str,
        value: float,
        identifier: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> Any:
        with tracer.start_as_current_span(
            "stripe.create_meter_event",
            attributes={"event_name": event_name, "value": value},
        ):
            params: dict[str, Any] = {
                "event_name": event_name,
                "payload": {
                    "value": str(value),
                    "stripe_customer_id": stripe_customer_id,
                },
            }
            if identifier:
                params["identifier"] = identifier
            if timestamp:
                params["timestamp"] = timestamp

            event = await StripeClient._call(stripe.billing.MeterEvent.create, **params)
            logger.info(
                "Created meter event %s for customer %s",
                event_name,
                stripe_customer_id,
            )
            return event

    @staticmethod
    async def get_meter_event_summary(
        customer_id: str,
        meter_id: str,
        start_time: int,
        end_time: int,
    ) -> Any:
        with tracer.start_as_current_span("stripe.get_meter_summary"):
            return await StripeClient._call(
                stripe.billing.Meter.list_event_summaries,
                meter_id,
                customer=customer_id,
                start_time=start_time,
                end_time=end_time,
            )

    @staticmethod
    async def get_invoice(invoice_id: str) -> stripe.Invoice:
        with tracer.start_as_current_span("stripe.get_invoice"):
            return await StripeClient._call(stripe.Invoice.retrieve, invoice_id)

    @staticmethod
    async def list_invoices(
        customer_id: str,
        status: Optional[str] = None,
        limit: int = 10,
    ) -> list[stripe.Invoice]:
        with tracer.start_as_current_span("stripe.list_invoices"):
            params: dict[str, Any] = {"customer": customer_id, "limit": limit}
            if status:
                params["status"] = status
            invoices = await StripeClient._call(stripe.Invoice.list, **params)
            return invoices.data

    @staticmethod
    async def send_invoice(invoice_id: str) -> stripe.Invoice:
        with tracer.start_as_current_span("stripe.send_invoice"):
            invoice = await StripeClient._call(stripe.Invoice.send_invoice, invoice_id)
            logger.info("Sent invoice %s", invoice_id)
            return invoice

    @staticmethod
    async def mark_invoice_paid_out_of_band(invoice_id: str) -> stripe.Invoice:
        with tracer.start_as_current_span("stripe.mark_paid_out_of_band"):
            invoice = await StripeClient._call(
                stripe.Invoice.pay,
                invoice_id,
                paid_out_of_band=True,
            )
            logger.info("Marked invoice %s as paid out of band", invoice_id)
            return invoice

    @staticmethod
    async def void_invoice(invoice_id: str) -> stripe.Invoice:
        with tracer.start_as_current_span("stripe.void_invoice"):
            invoice = await StripeClient._call(stripe.Invoice.void_invoice, invoice_id)
            logger.info("Voided invoice %s", invoice_id)
            return invoice

    @staticmethod
    async def list_overdue_invoices() -> list[stripe.Invoice]:
        import time

        with tracer.start_as_current_span("stripe.list_overdue_invoices"):
            invoices = await StripeClient._call(
                stripe.Invoice.list,
                status="open",
                due_date={"lt": int(time.time())},
            )
            return invoices.data

    @staticmethod
    async def create_checkout_session(
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        mode: str = "subscription",
        metadata: Optional[dict] = None,
        line_items: Optional[list[dict[str, Any]]] = None,
    ) -> stripe.checkout.Session:
        with tracer.start_as_current_span("stripe.create_checkout"):
            session = await StripeClient._call(
                stripe.checkout.Session.create,
                customer=customer_id,
                line_items=line_items or [{"price": price_id, "quantity": 1}],
                mode=mode,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=metadata or {},
            )
            logger.info(
                "Created checkout session %s for customer %s",
                session.id,
                customer_id,
            )
            return session

    @staticmethod
    async def list_checkout_session_line_items(
        session_id: str,
        *,
        expand: Optional[list[str]] = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if expand:
            params["expand"] = expand
        return await StripeClient._call(
            stripe.checkout.Session.list_line_items,
            session_id,
            **params,
        )

    @staticmethod
    async def create_portal_session(
        customer_id: str,
        return_url: str,
    ) -> stripe.billing_portal.Session:
        with tracer.start_as_current_span("stripe.create_portal_session"):
            return await StripeClient._call(
                stripe.billing_portal.Session.create,
                customer=customer_id,
                return_url=return_url,
            )

    @staticmethod
    async def create_customer_session(customer_id: str) -> Any:
        with tracer.start_as_current_span("stripe.create_customer_session"):
            return await StripeClient._call(
                stripe.CustomerSession.create,
                customer=customer_id,
                components={"pricing_table": {"enabled": True}},
            )

    @staticmethod
    async def list_products(active: bool = True) -> list[stripe.Product]:
        with tracer.start_as_current_span("stripe.list_products"):
            return (await StripeClient._call(stripe.Product.list, active=active)).data

    @staticmethod
    async def list_prices(
        product_id: Optional[str] = None,
        active: bool = True,
    ) -> list[stripe.Price]:
        with tracer.start_as_current_span("stripe.list_prices"):
            params: dict[str, Any] = {"active": active}
            if product_id:
                params["product"] = product_id
            return (await StripeClient._call(stripe.Price.list, **params)).data

    @staticmethod
    async def get_price(
        price_id: str,
        *,
        expand: Optional[list[str]] = None,
    ) -> stripe.Price:
        params: dict[str, Any] = {}
        if expand:
            params["expand"] = expand
        return await StripeClient._call(stripe.Price.retrieve, price_id, **params)

    @staticmethod
    def construct_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
        return stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )


stripe_client = StripeClient()

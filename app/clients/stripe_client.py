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
    async def get_customer_by_workspace(workspace_id: str) -> Optional[stripe.Customer]:
        with tracer.start_as_current_span("stripe.get_customer_by_workspace"):
            result = await StripeClient._call(
                stripe.Customer.search,
                query=f"metadata['workspace_id']:'{workspace_id}'",
            )
            return result.data[0] if result.data else None

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

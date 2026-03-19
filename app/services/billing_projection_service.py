"""Stripe event projection pipeline for billing-owned state."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.clients.stripe_client import stripe_client
from app.core.logging import logger, tracer
from app.repositories.billing_repository import billing_repository
from app.services.entitlement_service import invalidate_entitlements


CLIENT_REFERENCE_PATTERN = re.compile(r"^([a-f0-9-]+)_([a-f0-9-]+)$", re.IGNORECASE)

HANDLED_STRIPE_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
}


async def process_stripe_event(event: dict[str, Any]) -> None:
    """Normalize inbound Stripe events and update billing projections."""
    normalized = _normalize_event(event)
    event_id = normalized["event_id"]
    event_type = normalized["event_type"]
    payload = normalized["payload"]
    data_object = normalized["data_object"]

    with tracer.start_as_current_span(
        "billing.process_stripe_event",
        attributes={"stripe.event_id": event_id, "stripe.event_type": event_type},
    ):
        if not event_id or not event_type:
            logger.warning(
                "Skipping Stripe event with missing identifiers body=%s",
                _serialize_json(event),
            )
            return

        logger.info(
            "Processing Stripe event event_id=%s event_type=%s payload=%s",
            event_id,
            event_type,
            _serialize_json(payload),
        )

        if await billing_repository.is_webhook_processed(event_id):
            logger.info("Stripe event already processed event_id=%s", event_id)
            return

        if event_type not in HANDLED_STRIPE_EVENTS:
            logger.info(
                "Ignoring unsupported Stripe event type event_id=%s event_type=%s",
                event_id,
                event_type,
            )
            await billing_repository.mark_webhook_processed(event_id, event_type, payload)
            return

        try:
            if event_type == "checkout.session.completed":
                await _handle_checkout_completed(data_object)
            elif event_type == "customer.subscription.created":
                await _handle_subscription_created(data_object)
            elif event_type == "customer.subscription.updated":
                await _handle_subscription_updated(data_object)
            elif event_type == "customer.subscription.deleted":
                await _handle_subscription_deleted(data_object)
            elif event_type == "invoice.paid":
                await _handle_invoice_paid(data_object)
            elif event_type == "invoice.payment_failed":
                await _handle_invoice_payment_failed(data_object)

            await billing_repository.mark_webhook_processed(event_id, event_type, payload)
        except Exception:
            logger.error(
                "Failed to process Stripe event event_id=%s event_type=%s payload=%s",
                event_id,
                event_type,
                _serialize_json(payload),
                exc_info=True,
            )
            raise


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    if isinstance(event.get("detail"), dict):
        payload = dict(event["detail"])
        event_id = payload.get("id") or event.get("id") or event.get("event_id")
        event_type = payload.get("type") or event.get("detail-type") or event.get("event_type")
    elif "event_type" in event and "data" in event:
        payload = dict(event)
        event_id = event.get("event_id") or event.get("id")
        event_type = event.get("event_type") or event.get("type")
    else:
        payload = dict(event)
        event_id = event.get("id") or event.get("event_id")
        event_type = event.get("type") or event.get("event_type")

    data = payload.get("data") or {}
    if isinstance(data, dict):
        data_object = data.get("object", data)
    else:
        data_object = payload.get("object", {})

    return {
        "event_id": str(event_id or ""),
        "event_type": str(event_type or ""),
        "payload": payload,
        "data_object": data_object or {},
    }


def _serialize_json(value: Any) -> str:
    return json.dumps(value, default=str)


async def _handle_checkout_completed(session: dict[str, Any]) -> None:
    if session.get("mode") != "subscription" or not session.get("subscription"):
        logger.info("Skipping non-subscription checkout completion", extra={"session_id": session.get("id")})
        return

    subscription_id = _extract_id(session.get("subscription"))
    if not subscription_id:
        return

    subscription = await stripe_client.get_subscription(
        subscription_id,
        expand=["items.data.price.product", "customer"],
    )

    metadata = {
        **(session.get("metadata") or {}),
        **(subscription.get("metadata") or {}),
    }
    workspace_id = metadata.get("workspace_id")
    user_id = metadata.get("user_id")

    if (not workspace_id or not user_id) and session.get("client_reference_id"):
        match = CLIENT_REFERENCE_PATTERN.match(session["client_reference_id"])
        if match:
            workspace_id = workspace_id or match.group(1)
            user_id = user_id or match.group(2)

    if not workspace_id:
        logger.warning("Checkout completed without workspace metadata", extra={"session_id": session.get("id")})
        return

    await _project_subscription(
        subscription,
        workspace_id=workspace_id,
        user_id=user_id,
        create_quota_if_missing=True,
    )


async def _handle_subscription_created(subscription: dict[str, Any]) -> None:
    workspace_id = await _resolve_workspace_id_for_subscription(subscription)
    if not workspace_id:
        logger.info("Skipping subscription.created without workspace mapping", extra={"subscription_id": subscription.get("id")})
        return

    await _project_subscription(
        subscription,
        workspace_id=workspace_id,
        user_id=(subscription.get("metadata") or {}).get("user_id"),
        create_quota_if_missing=True,
    )


async def _handle_subscription_updated(subscription: dict[str, Any]) -> None:
    workspace_id = await _resolve_workspace_id_for_subscription(subscription)
    if not workspace_id:
        logger.warning("No workspace mapping for subscription.updated", extra={"subscription_id": subscription.get("id")})
        return

    await _project_subscription(
        subscription,
        workspace_id=workspace_id,
        user_id=(subscription.get("metadata") or {}).get("user_id"),
        create_quota_if_missing=False,
    )
    await invalidate_entitlements(workspace_id)


async def _handle_subscription_deleted(subscription: dict[str, Any]) -> None:
    workspace_id = await _resolve_workspace_id_for_subscription(subscription)
    if not workspace_id:
        return

    canceled_at = _from_unix(subscription.get("canceled_at")) or datetime.now(timezone.utc)
    await billing_repository.update_subscription_projection(
        str(subscription["id"]),
        status="canceled",
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
        cancel_at=_from_unix(subscription.get("cancel_at")),
        canceled_at=canceled_at,
    )
    await billing_repository.update_quota_status(str(subscription["id"]), "canceled")
    await invalidate_entitlements(workspace_id)


async def _handle_invoice_paid(invoice: dict[str, Any]) -> None:
    subscription_id = _extract_id(invoice.get("subscription"))
    if not subscription_id:
        logger.info("Ignoring non-subscription invoice.paid", extra={"invoice_id": invoice.get("id")})
        return

    subscription = await stripe_client.get_subscription(
        subscription_id,
        expand=["items.data.price.product", "customer"],
    )
    workspace_id = await _resolve_workspace_id_for_subscription(subscription)
    if not workspace_id:
        logger.warning("No workspace mapping for invoice.paid", extra={"invoice_id": invoice.get("id"), "subscription_id": subscription_id})
        return

    await _project_subscription(
        subscription,
        workspace_id=workspace_id,
        user_id=(subscription.get("metadata") or {}).get("user_id"),
        create_quota_if_missing=True,
        force_active_status=True,
    )
    await invalidate_entitlements(workspace_id)


async def _handle_invoice_payment_failed(invoice: dict[str, Any]) -> None:
    subscription_id = _extract_id(invoice.get("subscription"))
    if not subscription_id:
        return

    subscription_projection = await billing_repository.get_subscription_projection_by_id(subscription_id)
    if not subscription_projection:
        return

    await billing_repository.update_subscription_projection(subscription_id, status="past_due")
    await billing_repository.update_quota_status(subscription_id, "past_due")
    await invalidate_entitlements(str(subscription_projection["workspace_id"]))


async def _project_subscription(
    subscription: dict[str, Any],
    *,
    workspace_id: str,
    user_id: str | None,
    create_quota_if_missing: bool,
    force_active_status: bool = False,
) -> None:
    customer_id = _extract_id(subscription.get("customer"))
    if customer_id:
        await billing_repository.upsert_customer_mapping(
            workspace_id,
            customer_id,
            str(subscription["id"]),
        )
        await billing_repository.set_workspace_billing_provider(workspace_id, "stripe")

    status = "active" if force_active_status else _map_subscription_status(subscription.get("status"))
    period_start, period_end = _get_subscription_period(subscription)
    main_item = _get_main_subscription_item(subscription)
    main_price = main_item.get("price") if main_item else {}
    product = main_price.get("product") if isinstance(main_price, dict) else {}
    metadata = subscription.get("metadata") or {}

    await billing_repository.mark_other_subscriptions_canceled(workspace_id, str(subscription["id"]))
    await billing_repository.upsert_subscription_projection(
        {
            "id": str(subscription["id"]),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "stripe_customer_id": customer_id or "",
            "stripe_product_id": _extract_id(product),
            "stripe_price_id": _extract_id(main_price),
            "status": status,
            "billing_interval": ((main_price.get("recurring") or {}).get("interval") or "month"),
            "currency": str(subscription.get("currency") or main_price.get("currency") or "USD").upper(),
            "current_period_start": period_start,
            "current_period_end": period_end,
            "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
            "cancel_at": _from_unix(subscription.get("cancel_at")),
            "canceled_at": _from_unix(subscription.get("canceled_at")),
            "trial_end": _from_unix(subscription.get("trial_end")),
            "metadata": metadata,
        }
    )
    await billing_repository.update_quota_status(str(subscription["id"]), status)

    if create_quota_if_missing and not await billing_repository.quota_exists_for_period(str(subscription["id"]), period_start):
        await billing_repository.expire_active_quotas(str(subscription["id"]))
        for item in subscription.get("items", {}).get("data", []):
            quota = _quota_from_subscription_item(item, workspace_id, str(subscription["id"]), status, period_start, period_end)
            if quota:
                await billing_repository.create_quota(quota)


async def _resolve_workspace_id_for_subscription(subscription: dict[str, Any]) -> str | None:
    metadata = subscription.get("metadata") or {}
    workspace_id = metadata.get("workspace_id")
    if workspace_id:
        return str(workspace_id)

    existing = await billing_repository.get_subscription_projection_by_id(str(subscription["id"]))
    if existing:
        return str(existing["workspace_id"])

    customer_id = _extract_id(subscription.get("customer"))
    if customer_id:
        return await billing_repository.get_workspace_by_customer(customer_id)
    return None


def _quota_from_subscription_item(
    item: dict[str, Any],
    workspace_id: str,
    subscription_id: str,
    status: str,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any] | None:
    price = item.get("price") or {}
    product = price.get("product") or {}
    metadata = product.get("metadata") or {}

    total_credits = 0
    if metadata.get("total_credits"):
        total_credits = int(metadata["total_credits"])
    elif metadata.get("credits_per_unit"):
        total_credits = int(metadata["credits_per_unit"]) * int(item.get("quantity") or 1)

    if total_credits <= 0:
        return None

    priority = int(
        metadata.get("priority")
        or (3 if metadata.get("type") == "addon" else 2)
    )

    return {
        "workspace_id": workspace_id,
        "subscription_id": subscription_id,
        "total_credits": total_credits,
        "used_credits": 0,
        "status": status,
        "quota_start_date": period_start,
        "quota_end_date": period_end,
        "priority": priority,
    }


def _get_main_subscription_item(subscription: dict[str, Any]) -> dict[str, Any] | None:
    for item in subscription.get("items", {}).get("data", []):
        recurring = (item.get("price") or {}).get("recurring") or {}
        if recurring.get("usage_type") != "metered":
            return item
    items = subscription.get("items", {}).get("data", [])
    return items[0] if items else None


def _get_subscription_period(subscription: dict[str, Any]) -> tuple[datetime, datetime]:
    first_item = _get_main_subscription_item(subscription) or {}
    if first_item.get("current_period_start") and first_item.get("current_period_end"):
        return (
            _from_unix(first_item["current_period_start"]) or datetime.now(timezone.utc),
            _from_unix(first_item["current_period_end"]) or datetime.now(timezone.utc),
        )

    anchor = _from_unix(subscription.get("billing_cycle_anchor")) or datetime.now(timezone.utc)
    recurring = ((first_item.get("price") or {}).get("recurring") or {})
    interval = recurring.get("interval") or "month"
    interval_count = int(recurring.get("interval_count") or 1)
    if interval == "year":
        period_end = anchor + timedelta(days=365 * interval_count)
    elif interval == "month":
        period_end = anchor + timedelta(days=30 * interval_count)
    elif interval == "week":
        period_end = anchor + timedelta(days=7 * interval_count)
    else:
        period_end = anchor + timedelta(days=interval_count)
    return anchor, period_end


def _map_subscription_status(status: Any) -> str:
    status_map = {
        "active": "active",
        "canceled": "canceled",
        "incomplete": "pending",
        "incomplete_expired": "canceled",
        "past_due": "past_due",
        "paused": "paused",
        "trialing": "trialing",
        "unpaid": "past_due",
    }
    return status_map.get(str(status or "").lower(), str(status or "active").lower())


def _extract_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)


def _from_unix(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)

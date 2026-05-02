"""Read and session APIs for billing-engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.clients.stripe_client import stripe_client
from app.core.logging import logger, tracer
from app.repositories.billing_repository import billing_repository
from app.services.entitlement_service import get_entitlements
from app.services.billing_usage_service import check_usage_eligibility


DEFAULT_FEATURES_BY_TIER = {
    "free": ["ai_chat"],
    "starter": ["ai_chat", "whatsapp", "email_campaigns"],
    "growth": ["ai_chat", "whatsapp", "email_campaigns", "automations", "custom_actions"],
    "enterprise": [
        "ai_chat",
        "whatsapp",
        "email_campaigns",
        "automations",
        "custom_actions",
        "dedicated_support",
    ],
}


async def get_billing_summary(workspace_id: str) -> dict[str, Any]:
    with tracer.start_as_current_span("billing.get_summary", attributes={"workspace_id": workspace_id}):
        customer = await _get_or_backfill_customer_mapping(workspace_id)
        subscription = await billing_repository.get_subscription_projection(workspace_id)
        usage = await billing_repository.get_usage_snapshot(workspace_id)

        subscription_payload = None
        if subscription:
            subscription_payload = await _build_subscription_snapshot(subscription)

        recent_invoices: list[dict[str, Any]] = []
        if customer:
            try:
                invoices = await stripe_client.list_invoices(
                    customer_id=customer["stripe_customer_id"],
                    limit=10,
                )
                recent_invoices = [_invoice_to_dict(invoice) for invoice in invoices[:10]]
            except Exception as exc:
                logger.warning("Failed to fetch Stripe invoices for billing summary: %s", exc)

        total_allocated = usage["total_allocated"]
        total_used = usage["total_used"]
        overage = max(total_used - total_allocated, 0)
        usage_percentage = (total_used / total_allocated * 100) if total_allocated > 0 else 0

        return {
            "workspace_id": workspace_id,
            "has_customer": bool(customer),
            "overage_enabled": bool(customer),
            "subscription": subscription_payload,
            "usage": {
                "total_allocated": total_allocated,
                "total_used": total_used,
                "overage": overage,
                "usage_percentage": usage_percentage,
            },
            "recent_invoices": recent_invoices,
        }


async def get_plans() -> dict[str, list[dict[str, Any]]]:
    with tracer.start_as_current_span("billing.get_plans"):
        products = await stripe_client.list_products(active=True)
        prices = await stripe_client.list_prices(active=True)

        price_by_product: dict[str, list[dict[str, Any]]] = {}
        for price in prices:
            product_id = _extract_id(price.get("product")) or ""
            if not product_id:
                continue
            price_by_product.setdefault(product_id, []).append(
                {
                    "price_id": str(price["id"]),
                    "unit_amount": int(price.get("unit_amount") or 0),
                    "currency": str(price.get("currency") or "").lower(),
                    "interval": ((price.get("recurring") or {}).get("interval") or ""),
                    "interval_count": int(((price.get("recurring") or {}).get("interval_count") or 0)),
                    "type": str(price.get("type") or ""),
                }
            )

        response = {"monthly": [], "yearly": [], "one_time": []}
        for product in products:
            metadata = dict(product.get("metadata") or {})
            parsed_features = _parse_features(metadata.get("features"))
            tier = metadata.get("tier", "starter")

            for price in price_by_product.get(str(product["id"]), []):
                entry = {
                    "product_id": str(product["id"]),
                    "name": str(product.get("name") or ""),
                    "description": product.get("description"),
                    "tier": tier,
                    "features": parsed_features or DEFAULT_FEATURES_BY_TIER.get(tier, []),
                    "metadata": metadata,
                    "price": price,
                }
                if price["type"] == "one_time":
                    response["one_time"].append(entry)
                elif price["interval"] == "month":
                    response["monthly"].append(entry)
                elif price["interval"] == "year":
                    response["yearly"].append(entry)

        for bucket in response.values():
            bucket.sort(key=lambda item: int(item["metadata"].get("priority", "99")))

        return response


async def get_invoices(
    workspace_id: str,
    *,
    status: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    with tracer.start_as_current_span("billing.get_invoices", attributes={"workspace_id": workspace_id}):
        customer = await _get_or_backfill_customer_mapping(workspace_id)
        if not customer:
            return []

        invoices = await stripe_client.list_invoices(
            customer_id=customer["stripe_customer_id"],
            status=status or None,
            limit=limit,
        )
        return [_invoice_to_dict(invoice) for invoice in invoices]


async def create_portal_session(workspace_id: str, return_url: str) -> dict[str, str]:
    customer = await _get_or_backfill_customer_mapping(workspace_id)
    if not customer:
        raise ValueError("No Stripe customer found for workspace")

    session = await stripe_client.create_portal_session(
        customer["stripe_customer_id"],
        return_url,
    )
    return {"url": str(session["url"])}


async def create_customer_session(workspace_id: str) -> dict[str, str]:
    customer = await _get_or_backfill_customer_mapping(workspace_id)
    if not customer:
        return {"client_secret": ""}

    session = await stripe_client.create_customer_session(customer["stripe_customer_id"])
    return {"client_secret": str(session.get("client_secret") or "")}


async def check_entitlement(workspace_id: str, feature: str) -> dict[str, Any]:
    entitlements = await get_entitlements(workspace_id)
    if not entitlements.has_active_subscription:
        return {
            "workspace_id": workspace_id,
            "feature": feature,
            "has_access": False,
            "reason": "no_active_subscription",
        }

    has_access = feature in entitlements.features if feature else True
    return {
        "workspace_id": workspace_id,
        "feature": feature,
        "has_access": has_access,
        "reason": "" if has_access else "feature_not_included",
    }


async def check_usage(workspace_id: str, event_type: str, value: float) -> dict[str, Any]:
    decision = await check_usage_eligibility(workspace_id, event_type, value)
    return {
        "workspace_id": workspace_id,
        "event_type": event_type,
        "allowed": decision.allowed,
        "mode": decision.mode,
        "available_credits": decision.available_credits,
        "overage_enabled": decision.overage_enabled,
        "quota_ids": decision.quota_ids,
        "reason": decision.reason,
    }


async def _build_subscription_snapshot(subscription: dict[str, Any]) -> dict[str, Any]:
    price_id = subscription.get("stripe_price_id")
    product_name = "Unknown Plan"
    features: list[str] = []
    price_amount = 0
    currency = str(subscription.get("currency") or "usd").lower()
    interval = str(subscription.get("billing_interval") or "month")

    if price_id:
        try:
            price = await stripe_client.get_price(str(price_id), expand=["product"])
            product = price.get("product") if isinstance(price, dict) else getattr(price, "product", {})
            product_name = str((product or {}).get("name") or product_name)
            features = _parse_features(((product or {}).get("metadata") or {}).get("features"))
            price_amount = int(price.get("unit_amount") or 0)
            currency = str(price.get("currency") or currency).lower()
            interval = str(((price.get("recurring") or {}).get("interval") or interval))
        except Exception as exc:
            logger.warning("Failed to hydrate Stripe price %s: %s", price_id, exc)

    return {
        "subscription_id": str(subscription["id"]),
        "workspace_id": str(subscription["workspace_id"]),
        "status": str(subscription.get("status") or ""),
        "plan_name": product_name,
        "product_id": str(subscription.get("stripe_product_id") or ""),
        "price_id": str(price_id or ""),
        "price_amount": price_amount,
        "currency": currency,
        "interval": interval,
        "current_period_start": subscription.get("current_period_start"),
        "current_period_end": subscription.get("current_period_end"),
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
        "cancel_at": subscription.get("cancel_at"),
        "trial_end": subscription.get("trial_end"),
        "features": features,
    }


async def _get_or_backfill_customer_mapping(workspace_id: str) -> dict[str, Any] | None:
    customer = await billing_repository.get_customer_mapping(workspace_id)
    if customer:
        return customer

    stripe_customer = await stripe_client.get_customer_by_workspace(workspace_id)
    if not stripe_customer:
        return None

    await billing_repository.upsert_customer_mapping(
        workspace_id,
        stripe_customer["id"],
        stripe_subscription_id=None,
    )
    return await billing_repository.get_customer_mapping(workspace_id)


def _invoice_to_dict(invoice: dict[str, Any]) -> dict[str, Any]:
    invoice_subscription = invoice.get("subscription")
    if isinstance(invoice_subscription, dict):
        subscription_id = str(invoice_subscription.get("id") or "")
    else:
        subscription_id = str(invoice_subscription or "")

    return {
        "invoice_id": str(invoice["id"]),
        "number": str(invoice.get("number") or ""),
        "status": str(invoice.get("status") or ""),
        "amount_due": int(invoice.get("amount_due") or 0),
        "amount_paid": int(invoice.get("amount_paid") or 0),
        "currency": str(invoice.get("currency") or "").lower(),
        "description": invoice.get("description"),
        "created_at": datetime.fromtimestamp(int(invoice["created"]), tz=timezone.utc),
        "due_date": datetime.fromtimestamp(int(invoice["due_date"]), tz=timezone.utc) if invoice.get("due_date") else None,
        "invoice_pdf": invoice.get("invoice_pdf"),
        "hosted_invoice_url": invoice.get("hosted_invoice_url"),
        "subscription_id": subscription_id,
        "billing_reason": str(invoice.get("billing_reason") or ""),
    }


def _parse_features(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _extract_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)

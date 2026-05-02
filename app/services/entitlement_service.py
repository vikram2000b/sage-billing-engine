"""Entitlement service — cached entitlement reads backed by billing-engine state.

Cache hierarchy:
  L1: Redis (TTL-based, invalidated by billing events)
  L2: Billing projections + quota state, with selective Stripe catalog hydration

Callers can pass `refresh=True` to bypass the cache.
"""

from datetime import datetime, timezone
import json
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import logger, tracer
from app.core.redis import redis_client
from app.clients.stripe_client import stripe_client
from app.models.enums import PlanTier, SubscriptionStatus
from app.models.schemas import EntitlementResponse, UsageSummary
from app.repositories.billing_repository import billing_repository

# Redis key prefixes
ENTITLEMENT_KEY = "entitlements:{workspace_id}"
USAGE_COUNTER_KEY = "usage:{workspace_id}:{meter}"

# Feature map: which features are available on which plan tiers
# This is the fallback; prefer Stripe product metadata when available.
PLAN_FEATURES: dict[PlanTier, list[str]] = {
    PlanTier.FREE: [
        "ai_chat",
    ],
    PlanTier.STARTER: [
        "ai_chat",
        "whatsapp",
        "email_campaigns",
    ],
    PlanTier.GROWTH: [
        "ai_chat",
        "whatsapp",
        "email_campaigns",
        "automations",
        "custom_actions",
    ],
    PlanTier.ENTERPRISE: [
        "ai_chat",
        "whatsapp",
        "email_campaigns",
        "automations",
        "custom_actions",
        "dedicated_support",
        "sla",
        "custom_integrations",
    ],
}

ACTIVE_ENTITLEMENT_STATUSES = {
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.PAST_DUE,
}

USAGE_LIMIT_METADATA_KEYS: dict[str, tuple[str, ...]] = {
    "ai_credits": ("ai_credits_limit",),
    "whatsapp_message": ("whatsapp_message_limit", "whatsapp_messages_limit"),
    "email_send": ("email_send_limit", "email_sends_limit"),
}

USAGE_METER_ALIASES = {
    "whatsapp_messages": "whatsapp_message",
    "email_sends": "email_send",
}


async def get_entitlements(
    workspace_id: str,
    refresh: bool = False,
    ttl: Optional[int] = None,
) -> EntitlementResponse:
    """Get entitlements for a workspace.

    1. Check Redis cache (unless refresh=True)
    2. On miss, fetch from Stripe
    3. Cache the result in Redis
    """
    cache_ttl = ttl or settings.ENTITLEMENT_CACHE_TTL
    cache_key = ENTITLEMENT_KEY.format(workspace_id=workspace_id)

    with tracer.start_as_current_span("entitlement.check", attributes={
        "workspace_id": workspace_id,
        "refresh": refresh,
    }):
        # ── L1: Redis cache ──
        if not refresh:
            cached = await redis_client.get_cached_json(cache_key)
            if cached:
                logger.info(f"Entitlement cache hit for workspace {workspace_id}")
                response = EntitlementResponse(**cached)
                response.cached = True
                return response

        # ── L2: Billing projections ──
        logger.info(
            "Entitlement cache miss for workspace %s, fetching from billing state",
            workspace_id,
        )
        entitlements = await _fetch_entitlements_from_billing_state(workspace_id)

        # ── Write back to cache ──
        entitlements.cached_at = datetime.now(timezone.utc)
        await redis_client.set_cached_json(cache_key, entitlements.model_dump(mode="json"), cache_ttl)

        return entitlements


async def check_feature_access(workspace_id: str, feature: str) -> bool:
    """Quick check: does this workspace have access to a specific feature?"""
    entitlements = await get_entitlements(workspace_id)
    return feature in entitlements.features and entitlements.has_active_subscription


async def check_usage_limit(workspace_id: str, meter: str) -> bool:
    """Check if a workspace has exceeded its usage limit for a meter.

    Uses the real-time Redis counter (updated on every usage event)
    rather than Stripe's delayed aggregation.
    """
    with tracer.start_as_current_span("entitlement.check_usage_limit"):
        normalized_meter = USAGE_METER_ALIASES.get(meter, meter)
        entitlements = await get_entitlements(workspace_id)
        usage = entitlements.usage.get(normalized_meter)
        if not usage or usage.limit is None:
            return False  # No limit configured = not exceeded

        # Check real-time counter in Redis
        counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=normalized_meter)
        current_usage = await redis_client.get_float(counter_key)

        return current_usage >= usage.limit


async def invalidate_entitlements(workspace_id: str) -> None:
    """Invalidate the entitlement cache for a workspace.

    Called when a Stripe webhook indicates a subscription change.
    """
    cache_key = ENTITLEMENT_KEY.format(workspace_id=workspace_id)
    await redis_client.delete_cached(cache_key)
    logger.info(f"Invalidated entitlement cache for workspace {workspace_id}")


async def increment_usage_counter(workspace_id: str, meter: str, value: float) -> float:
    """Increment the real-time usage counter in Redis.

    Called by the usage event consumer after pushing to Stripe.
    Returns the new counter value.
    """
    counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=meter)
    new_value = await redis_client.increment_float(counter_key, value)
    logger.info(f"Usage counter {meter} for workspace {workspace_id}: {new_value}")
    return new_value


async def reset_usage_counter(workspace_id: str, meter: str, value: float = 0.0) -> None:
    """Reset a usage counter (e.g. on billing period rollover)."""
    counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=meter)
    await redis_client.set_with_ttl(counter_key, str(value), ttl_seconds=86400 * 35)  # 35 days


# ─── Private helpers ───


async def _fetch_entitlements_from_billing_state(workspace_id: str) -> EntitlementResponse:
    """Fetch full entitlement data from billing projections and quota state."""
    customer = await billing_repository.get_customer_mapping(workspace_id)
    subscription = await billing_repository.get_subscription_projection(workspace_id)

    if not customer and not subscription:
        logger.info("No billing customer or subscription found for workspace %s", workspace_id)
        return EntitlementResponse(
            workspace_id=workspace_id,
            has_active_subscription=False,
            plan_tier=PlanTier.FREE,
            subscription_status=None,
            features=PLAN_FEATURES[PlanTier.FREE],
            is_quota_exceeded=False,
        )

    if not subscription:
        logger.info("No projected subscription for workspace %s", workspace_id)
        return EntitlementResponse(
            workspace_id=workspace_id,
            has_active_subscription=False,
            plan_tier=PlanTier.FREE,
            subscription_status=SubscriptionStatus.CANCELED,
            features=PLAN_FEATURES[PlanTier.FREE],
            stripe_customer_id=(customer or {}).get("stripe_customer_id"),
        )

    try:
        sub_status = SubscriptionStatus(str(subscription.get("status") or "active"))
    except ValueError:
        sub_status = SubscriptionStatus.ACTIVE

    product_metadata = await _load_product_metadata(subscription)
    plan_tier = _parse_plan_tier(product_metadata.get("tier"))
    features = _parse_features(product_metadata.get("features")) or PLAN_FEATURES.get(plan_tier, [])

    usage_snapshot = await billing_repository.get_usage_snapshot(workspace_id)
    usage = await _build_usage_summary(
        workspace_id=workspace_id,
        product_metadata=product_metadata,
        usage_snapshot=usage_snapshot,
    )
    total_allocated = float(usage_snapshot.get("total_allocated") or 0)
    total_used = float(usage_snapshot.get("total_used") or 0)

    payment_overdue = sub_status == SubscriptionStatus.PAST_DUE
    is_quota_exceeded = (
        (total_allocated > 0 and total_used >= total_allocated)
        or any((u.limit is not None and u.used >= u.limit) for u in usage.values())
    )

    return EntitlementResponse(
        workspace_id=workspace_id,
        has_active_subscription=sub_status in ACTIVE_ENTITLEMENT_STATUSES,
        plan_tier=plan_tier,
        subscription_status=sub_status,
        features=features,
        usage=usage,
        is_quota_exceeded=is_quota_exceeded,
        payment_overdue=payment_overdue,
        stripe_customer_id=(customer or {}).get("stripe_customer_id") or subscription.get("stripe_customer_id"),
    )


async def _load_product_metadata(subscription: dict[str, Any]) -> dict[str, Any]:
    price_id = subscription.get("stripe_price_id")
    if not price_id:
        return {}

    try:
        price = await stripe_client.get_price(str(price_id), expand=["product"])
    except Exception as exc:
        logger.warning(
            "Failed to hydrate Stripe price %s for entitlements: %s",
            price_id,
            exc,
        )
        return {}

    product = price.get("product") if isinstance(price, dict) else getattr(price, "product", {})
    metadata = (product or {}).get("metadata") or {}
    return dict(metadata)


async def _build_usage_summary(
    *,
    workspace_id: str,
    product_metadata: dict[str, Any],
    usage_snapshot: dict[str, float],
) -> dict[str, UsageSummary]:
    usage: dict[str, UsageSummary] = {}

    for meter_name, metadata_keys in USAGE_LIMIT_METADATA_KEYS.items():
        limit_str = next(
            (
                product_metadata.get(metadata_key)
                for metadata_key in metadata_keys
                if product_metadata.get(metadata_key) not in (None, "")
            ),
            None,
        )
        if limit_str is None:
            continue

        try:
            limit = float(limit_str)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid usage limit metadata for meter %s: %r",
                meter_name,
                limit_str,
            )
            continue

        counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=meter_name)
        current_used = await redis_client.get_float(counter_key)
        if meter_name == "ai_credits":
            current_used = max(current_used, float(usage_snapshot.get("total_used") or 0))

        usage[meter_name] = UsageSummary(
            used=current_used,
            limit=limit,
            percentage=(current_used / limit * 100) if limit > 0 else 0,
        )

    return usage


def _parse_plan_tier(raw_tier: Any) -> PlanTier:
    try:
        return PlanTier(str(raw_tier or "starter"))
    except ValueError:
        return PlanTier.STARTER


def _parse_features(raw_features: Any) -> list[str]:
    if not raw_features:
        return []
    if isinstance(raw_features, list):
        return [str(item) for item in raw_features]
    if isinstance(raw_features, str):
        try:
            parsed = json.loads(raw_features)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None

        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [item.strip() for item in raw_features.split(",") if item.strip()]
    return []

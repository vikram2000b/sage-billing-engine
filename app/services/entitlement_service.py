"""Entitlement service — checks whether a workspace has access to features and usage limits.

Cache hierarchy:
  L1: Redis (TTL-based, invalidated by Stripe webhooks)
  L2: Stripe API (source of truth)

Callers can pass `refresh=True` to bypass the cache.
"""

from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logging import logger, tracer
from app.core.redis import redis_client
from app.clients.stripe_client import stripe_client
from app.models.enums import PlanTier, SubscriptionStatus
from app.models.schemas import EntitlementResponse, UsageSummary

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

        # ── L2: Stripe API ──
        logger.info(f"Entitlement cache miss for workspace {workspace_id}, fetching from Stripe")
        entitlements = await _fetch_entitlements_from_stripe(workspace_id)

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
        entitlements = await get_entitlements(workspace_id)
        usage = entitlements.usage.get(meter)
        if not usage or usage.limit is None:
            return False  # No limit configured = not exceeded

        # Check real-time counter in Redis
        counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=meter)
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


async def _fetch_entitlements_from_stripe(workspace_id: str) -> EntitlementResponse:
    """Fetch full entitlement data from Stripe for a workspace."""
    customer = await stripe_client.get_customer_by_workspace(workspace_id)

    if not customer:
        logger.info(f"No Stripe customer found for workspace {workspace_id}")
        return EntitlementResponse(
            workspace_id=workspace_id,
            has_active_subscription=False,
            plan_tier=PlanTier.FREE,
            subscription_status=None,
            features=PLAN_FEATURES[PlanTier.FREE],
            is_quota_exceeded=False,
        )

    # Get active subscription
    subscription = await stripe_client.get_active_subscription(customer.id)

    if not subscription:
        logger.info(f"No active subscription for workspace {workspace_id}")
        return EntitlementResponse(
            workspace_id=workspace_id,
            has_active_subscription=False,
            plan_tier=PlanTier.FREE,
            subscription_status=SubscriptionStatus.CANCELED,
            features=PLAN_FEATURES[PlanTier.FREE],
            stripe_customer_id=customer.id,
        )

    # Determine plan tier from product metadata
    product = subscription["items"]["data"][0]["price"]["product"]
    if isinstance(product, str):
        import stripe as stripe_lib
        product_obj = stripe_lib.Product.retrieve(product)
    else:
        product_obj = product

    plan_tier_str = product_obj.get("metadata", {}).get("tier", "starter")
    try:
        plan_tier = PlanTier(plan_tier_str)
    except ValueError:
        plan_tier = PlanTier.STARTER

    # Get features from product metadata or fallback to PLAN_FEATURES
    features_str = product_obj.get("metadata", {}).get("features", "")
    if features_str:
        features = [f.strip() for f in features_str.split(",")]
    else:
        features = PLAN_FEATURES.get(plan_tier, [])

    # Get usage limits from product metadata
    usage: dict[str, UsageSummary] = {}
    for meter_name in ["ai_credits", "whatsapp_messages", "email_sends"]:
        limit_str = product_obj.get("metadata", {}).get(f"{meter_name}_limit")
        if limit_str:
            # Get real-time counter from Redis
            counter_key = USAGE_COUNTER_KEY.format(workspace_id=workspace_id, meter=meter_name)
            current_used = await redis_client.get_float(counter_key)
            limit = float(limit_str)
            usage[meter_name] = UsageSummary(
                used=current_used,
                limit=limit,
                percentage=(current_used / limit * 100) if limit > 0 else 0,
            )

    # Check for payment overdue
    payment_overdue = subscription.get("status") == "past_due"

    sub_status_str = subscription.get("status", "active")
    try:
        sub_status = SubscriptionStatus(sub_status_str)
    except ValueError:
        sub_status = SubscriptionStatus.ACTIVE

    is_quota_exceeded = any(
        (u.limit is not None and u.used >= u.limit) for u in usage.values()
    )

    return EntitlementResponse(
        workspace_id=workspace_id,
        has_active_subscription=True,
        plan_tier=plan_tier,
        subscription_status=sub_status,
        features=features,
        usage=usage,
        is_quota_exceeded=is_quota_exceeded,
        payment_overdue=payment_overdue,
        stripe_customer_id=customer.id,
    )

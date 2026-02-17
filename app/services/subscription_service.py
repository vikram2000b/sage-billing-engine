"""Subscription service — manages subscription lifecycle via Stripe.

All subscription state lives in Stripe. This service provides the
business logic layer between the API routes and the Stripe client.
"""

from typing import Optional, Any

from app.core.logging import logger, tracer
from app.clients.stripe_client import stripe_client
from app.services.entitlement_service import invalidate_entitlements
from app.models.schemas import (
    SubscriptionResponse,
    CreateSubscriptionRequest,
    CancelSubscriptionRequest,
    ChangeSubscriptionRequest,
)
from app.models.enums import PlanTier, SubscriptionStatus, PaymentCollectionMethod, PaymentGateway


async def get_subscription(workspace_id: str) -> Optional[SubscriptionResponse]:
    """Get the current subscription for a workspace."""
    with tracer.start_as_current_span("subscription.get", attributes={
        "workspace_id": workspace_id,
    }):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            return None

        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            return None

        return _stripe_sub_to_response(sub, workspace_id)


async def create_subscription(request: CreateSubscriptionRequest) -> SubscriptionResponse:
    """Create a new subscription for a workspace."""
    with tracer.start_as_current_span("subscription.create", attributes={
        "workspace_id": request.workspace_id,
    }):
        # Get or create customer
        customer = await stripe_client.get_or_create_customer(
            workspace_id=request.workspace_id,
            email=request.email,
            metadata={"user_id": request.user_id},
        )

        # Create subscription
        sub = await stripe_client.create_subscription(
            customer_id=customer.id,
            price_id=request.plan_price_id,
            collection_method=request.collection_method.value,
            trial_days=request.trial_days,
            metadata={
                "workspace_id": request.workspace_id,
                "user_id": request.user_id,
                **request.metadata,
            },
            preferred_gateway=request.preferred_gateway.value,
        )

        # Invalidate entitlement cache
        await invalidate_entitlements(request.workspace_id)

        logger.info(f"Created subscription {sub.id} for workspace {request.workspace_id}")
        return _stripe_sub_to_response(sub, request.workspace_id)


async def cancel_subscription(request: CancelSubscriptionRequest) -> SubscriptionResponse:
    """Cancel a workspace's subscription."""
    with tracer.start_as_current_span("subscription.cancel", attributes={
        "workspace_id": request.workspace_id,
    }):
        customer = await stripe_client.get_customer_by_workspace(request.workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {request.workspace_id}")

        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            raise ValueError(f"No active subscription for workspace {request.workspace_id}")

        canceled_sub = await stripe_client.cancel_subscription(
            sub.id,
            cancel_immediately=request.cancel_immediately,
        )

        # Invalidate entitlement cache
        await invalidate_entitlements(request.workspace_id)

        return _stripe_sub_to_response(canceled_sub, request.workspace_id)


async def change_plan(request: ChangeSubscriptionRequest) -> SubscriptionResponse:
    """Change the plan on an existing subscription."""
    with tracer.start_as_current_span("subscription.change_plan", attributes={
        "workspace_id": request.workspace_id,
    }):
        customer = await stripe_client.get_customer_by_workspace(request.workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {request.workspace_id}")

        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            raise ValueError(f"No active subscription for workspace {request.workspace_id}")

        updated_sub = await stripe_client.change_subscription_plan(
            sub.id,
            new_price_id=request.new_price_id,
            proration_behavior=request.proration_behavior,
        )

        # Invalidate entitlement cache
        await invalidate_entitlements(request.workspace_id)

        return _stripe_sub_to_response(updated_sub, request.workspace_id)


async def revoke_cancellation(workspace_id: str) -> SubscriptionResponse:
    """Revoke a pending end-of-period cancellation."""
    with tracer.start_as_current_span("subscription.revoke_cancellation"):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {workspace_id}")

        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            raise ValueError(f"No active subscription for workspace {workspace_id}")

        updated_sub = await stripe_client.revoke_cancellation(sub.id)
        await invalidate_entitlements(workspace_id)
        return _stripe_sub_to_response(updated_sub, workspace_id)


async def pause_subscription(workspace_id: str) -> SubscriptionResponse:
    """Pause a workspace's subscription."""
    with tracer.start_as_current_span("subscription.pause"):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {workspace_id}")

        sub = await stripe_client.get_active_subscription(customer.id)
        if not sub:
            raise ValueError(f"No active subscription for workspace {workspace_id}")

        paused_sub = await stripe_client.pause_subscription(sub.id)
        await invalidate_entitlements(workspace_id)
        return _stripe_sub_to_response(paused_sub, workspace_id)


async def resume_subscription(workspace_id: str) -> SubscriptionResponse:
    """Resume a paused subscription."""
    with tracer.start_as_current_span("subscription.resume"):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            raise ValueError(f"No Stripe customer found for workspace {workspace_id}")

        # For paused subs, we need to look at all statuses
        import stripe as stripe_lib
        subs = stripe_lib.Subscription.list(customer=customer.id, limit=1)
        if not subs.data:
            raise ValueError(f"No subscription found for workspace {workspace_id}")

        resumed_sub = await stripe_client.resume_subscription(subs.data[0].id)
        await invalidate_entitlements(workspace_id)
        return _stripe_sub_to_response(resumed_sub, workspace_id)


# ─── Helpers ───


def _stripe_sub_to_response(sub: Any, workspace_id: str) -> SubscriptionResponse:
    """Convert a Stripe Subscription object to our response schema."""
    from datetime import datetime, timezone

    metadata = dict(sub.get("metadata", {}))
    preferred_gw = metadata.pop("preferred_gateway", "stripe")

    try:
        plan_tier = PlanTier(metadata.get("plan_tier", "starter"))
    except ValueError:
        plan_tier = PlanTier.STARTER

    try:
        status = SubscriptionStatus(sub["status"])
    except ValueError:
        status = SubscriptionStatus.ACTIVE

    try:
        collection = PaymentCollectionMethod(sub.get("collection_method", "charge_automatically"))
    except ValueError:
        collection = PaymentCollectionMethod.CHARGE_AUTOMATICALLY

    try:
        gateway = PaymentGateway(preferred_gw)
    except ValueError:
        gateway = PaymentGateway.STRIPE

    return SubscriptionResponse(
        subscription_id=sub["id"],
        workspace_id=workspace_id,
        stripe_customer_id=sub["customer"],
        plan_tier=plan_tier,
        status=status,
        collection_method=collection,
        preferred_gateway=gateway,
        current_period_start=datetime.fromtimestamp(sub["current_period_start"], tz=timezone.utc),
        current_period_end=datetime.fromtimestamp(sub["current_period_end"], tz=timezone.utc),
        cancel_at_period_end=sub.get("cancel_at_period_end", False),
        trial_end=datetime.fromtimestamp(sub["trial_end"], tz=timezone.utc) if sub.get("trial_end") else None,
        metadata=metadata,
    )

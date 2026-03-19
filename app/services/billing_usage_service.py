"""Unified prepaid allocation and overage metering for billing events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.clients.stripe_client import stripe_client
from app.core.config import settings
from app.core.logging import logger, tracer
from app.core.redis import redis_client
from app.models.enums import UsageEventType
from app.repositories.billing_repository import billing_repository
from app.services.entitlement_service import increment_usage_counter


USAGE_EVENT_KEY = "billing:usage-event:{idempotency_key}"

EVENT_TYPE_TO_METER: dict[str, str] = {
    UsageEventType.AI_CREDITS.value: settings.STRIPE_METER_AI_CREDITS,
    UsageEventType.WHATSAPP_MESSAGE.value: settings.STRIPE_METER_WHATSAPP_MESSAGES,
    UsageEventType.EMAIL_SEND.value: settings.STRIPE_METER_EMAIL_SENDS,
}


@dataclass(slots=True)
class UsageDecision:
    allowed: bool
    mode: str
    prepaid_value: float
    overage_value: float
    allocations: list[dict[str, float]]
    available_credits: float
    overage_enabled: bool
    quota_ids: list[str]
    reason: str = ""
    stripe_meter_event_id: str = ""


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def check_usage_eligibility(
    workspace_id: str,
    event_type: str,
    value: float,
) -> UsageDecision:
    """Return a dry-run allocation decision for a billing event."""
    event = {
        "workspace_id": workspace_id,
        "event_type": event_type,
        "value": value,
        "metadata": {},
    }
    return await _plan_usage(event)


async def authorize_usage(event: dict[str, Any]) -> UsageDecision:
    """Return an allocation preview without mutating projections."""
    return await _plan_usage(event)


async def record_usage(event: dict[str, Any]) -> UsageDecision:
    """Apply prepaid allocation, then meter the residual overage to Stripe."""
    idempotency_key = event.get("idempotency_key")
    idempotency_cache_key = None

    if idempotency_key:
        idempotency_cache_key = USAGE_EVENT_KEY.format(idempotency_key=idempotency_key)
        acquired = await redis_client.set_if_not_exists(
            idempotency_cache_key,
            "1",
            settings.USAGE_EVENT_IDEMPOTENCY_TTL_SECONDS,
        )
        if not acquired:
            logger.info("Duplicate billing usage event skipped", extra={"event": event})
            return UsageDecision(
                allowed=True,
                mode="duplicate",
                prepaid_value=0,
                overage_value=0,
                allocations=[],
                available_credits=0,
                overage_enabled=True,
                quota_ids=[],
            )

    try:
        decision = await _plan_usage(event)
        if not decision.allowed:
            if idempotency_cache_key:
                await redis_client.delete_cached(idempotency_cache_key)
            return decision

        for allocation in decision.allocations:
            await billing_repository.increment_quota_usage(
                allocation["quota_id"],
                allocation["value"],
            )
            await billing_repository.insert_quota_transaction(
                allocation["quota_id"],
                allocation["value"],
                event.get("metadata", {}),
            )

        stripe_meter_event_id = ""
        if decision.overage_value > 0:
            customer = await billing_repository.get_customer_mapping(event["workspace_id"])
            if not customer:
                raise ValueError("No Stripe customer mapping available for overage metering")

            meter_name = EVENT_TYPE_TO_METER.get(event["event_type"])
            if not meter_name:
                raise ValueError(f"Unsupported billing event type: {event['event_type']}")

            occurred_at = _coerce_datetime(event.get("occurred_at"))
            meter_event = await stripe_client.create_meter_event(
                event_name=meter_name,
                stripe_customer_id=customer["stripe_customer_id"],
                value=decision.overage_value,
                identifier=idempotency_key or None,
                timestamp=int(occurred_at.timestamp()),
            )
            stripe_meter_event_id = (
                getattr(meter_event, "identifier", None)
                or meter_event.get("identifier", "")
            )

            await billing_repository.insert_usage_audit(
                workspace_id=event["workspace_id"],
                meter_type=event["event_type"],
                quantity=decision.overage_value,
                stripe_usage_record_id=stripe_meter_event_id or idempotency_key,
            )

        try:
            await increment_usage_counter(
                workspace_id=event["workspace_id"],
                meter=event["event_type"],
                value=float(event["value"]),
            )
        except Exception as exc:  # pragma: no cover - best effort cache update
            logger.warning("Failed to update billing usage counter: %s", exc)

        decision.stripe_meter_event_id = stripe_meter_event_id
        return decision
    except Exception:
        if idempotency_cache_key:
            await redis_client.delete_cached(idempotency_cache_key)
        raise


async def _plan_usage(event: dict[str, Any]) -> UsageDecision:
    workspace_id = str(event.get("workspace_id") or "")
    event_type = str(event.get("event_type") or "")
    requested_value = float(event.get("value") or 0)

    with tracer.start_as_current_span(
        "billing.usage.plan",
        attributes={
            "workspace_id": workspace_id,
            "event_type": event_type,
            "value": requested_value,
        },
    ):
        if not workspace_id or not event_type or requested_value <= 0:
            return UsageDecision(
                allowed=False,
                mode="blocked",
                prepaid_value=0,
                overage_value=0,
                allocations=[],
                available_credits=0,
                overage_enabled=False,
                quota_ids=[],
                reason="invalid_event",
            )

        quotas = await billing_repository.list_allocatable_quotas(workspace_id)
        customer = await billing_repository.get_customer_mapping(workspace_id)

        quota_ids = [str(quota["id"]) for quota in quotas]
        available_credits = sum(
            max(float(quota.get("total_credits") or 0) - float(quota.get("used_credits") or 0), 0)
            for quota in quotas
        )

        remaining = requested_value
        allocations: list[dict[str, float]] = []
        for quota in quotas:
            available = max(
                float(quota.get("total_credits") or 0) - float(quota.get("used_credits") or 0),
                0,
            )
            if available <= 0 or remaining <= 0:
                continue
            consumed = min(remaining, available)
            allocations.append({"quota_id": str(quota["id"]), "value": consumed})
            remaining -= consumed

        prepaid_value = requested_value - remaining
        overage_value = max(remaining, 0)
        overage_enabled = bool(customer and EVENT_TYPE_TO_METER.get(event_type))

        if overage_value > 0 and not overage_enabled:
            return UsageDecision(
                allowed=False,
                mode="blocked",
                prepaid_value=prepaid_value,
                overage_value=overage_value,
                allocations=allocations,
                available_credits=available_credits,
                overage_enabled=False,
                quota_ids=quota_ids,
                reason="no_overage_configuration",
            )

        mode = "overage" if overage_value > 0 else "quota"
        if prepaid_value <= 0 and overage_value <= 0:
            mode = "blocked"

        allowed = prepaid_value > 0 or overage_value > 0
        reason = ""
        if not allowed:
            reason = "no_available_credits"

        return UsageDecision(
            allowed=allowed,
            mode=mode,
            prepaid_value=prepaid_value,
            overage_value=overage_value,
            allocations=allocations,
            available_credits=available_credits,
            overage_enabled=overage_enabled,
            quota_ids=quota_ids,
            reason=reason,
        )

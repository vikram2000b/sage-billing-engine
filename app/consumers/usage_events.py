"""SQS consumer for internal billing usage events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.logging import logger, tracer
from app.services.billing_usage_service import record_usage


async def handle_usage_event(event: dict[str, Any]) -> None:
    with tracer.start_as_current_span(
        "consumer.usage_event",
        attributes={
            "event_type": event.get("event_type"),
            "workspace_id": event.get("workspace_id"),
        },
    ):
        workspace_id = str(event.get("workspace_id") or "")
        event_type = str(event.get("event_type") or "")
        value = float(event.get("value") or 0)

        if not workspace_id or not event_type or value <= 0:
            logger.warning("Invalid billing usage event, skipping", extra={"event": event})
            return

        occurred_at = event.get("occurred_at") or event.get("timestamp")
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        elif occurred_at is None:
            occurred_at = datetime.now(timezone.utc)

        decision = await record_usage(
            {
                "version": event.get("version") or "v1",
                "source_service": event.get("source_service") or "unknown",
                "workspace_id": workspace_id,
                "event_type": event_type,
                "value": value,
                "idempotency_key": event.get("idempotency_key"),
                "occurred_at": occurred_at,
                "metadata": event.get("metadata") or {},
            }
        )

        logger.info(
            "Processed billing usage event",
            extra={
                "workspace_id": workspace_id,
                "event_type": event_type,
                "mode": decision.mode,
                "prepaid_value": decision.prepaid_value,
                "overage_value": decision.overage_value,
            },
        )

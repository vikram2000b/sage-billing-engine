"""Billing gRPC service implementation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp

from app.core.logging import logger
from app.services import billing_projection_service  # noqa: F401 - imported for service readiness
from app.services.billing_service import (
    check_entitlement,
    check_usage,
    create_customer_session,
    create_portal_session,
    get_billing_summary,
    get_invoices,
    get_plans,
)
from app.services.billing_usage_service import authorize_usage, record_usage
from sagepilot.billing import billing_pb2, billing_pb2_grpc


class BillingServicer(billing_pb2_grpc.BillingServiceServicer):
    async def GetBillingSummary(self, request, context):
        summary = await get_billing_summary(request.workspace_id)
        response = billing_pb2.BillingSummaryResponse(
            workspace_id=summary["workspace_id"],
            has_customer=summary["has_customer"],
            overage_enabled=summary["overage_enabled"],
            usage=_usage_snapshot_to_proto(summary["usage"]),
            recent_invoices=[_invoice_to_proto(item) for item in summary["recent_invoices"]],
        )
        if summary.get("subscription"):
            response.subscription.CopyFrom(_subscription_to_proto(summary["subscription"]))
        return response

    async def GetPlans(self, request, context):
        plans = await get_plans()
        return billing_pb2.GetPlansResponse(
            monthly=[_plan_to_proto(item) for item in plans["monthly"]],
            yearly=[_plan_to_proto(item) for item in plans["yearly"]],
            one_time=[_plan_to_proto(item) for item in plans["one_time"]],
        )

    async def GetInvoices(self, request, context):
        invoices = await get_invoices(
            request.workspace_id,
            status=request.status or None,
        )
        return billing_pb2.GetInvoicesResponse(
            invoices=[_invoice_to_proto(item) for item in invoices],
        )

    async def CreatePortalSession(self, request, context):
        try:
            payload = await create_portal_session(request.workspace_id, request.return_url)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        return billing_pb2.CreatePortalSessionResponse(url=payload["url"])

    async def CreateCustomerSession(self, request, context):
        payload = await create_customer_session(request.workspace_id)
        return billing_pb2.CreateCustomerSessionResponse(client_secret=payload["client_secret"])

    async def CheckEntitlement(self, request, context):
        payload = await check_entitlement(request.workspace_id, request.feature)
        return billing_pb2.CheckEntitlementResponse(
            workspace_id=payload["workspace_id"],
            feature=payload["feature"],
            has_access=payload["has_access"],
            reason=payload["reason"],
        )

    async def CheckUsageEligibility(self, request, context):
        payload = await check_usage(request.workspace_id, request.event_type, request.value)
        return billing_pb2.CheckUsageEligibilityResponse(
            workspace_id=payload["workspace_id"],
            event_type=payload["event_type"],
            allowed=payload["allowed"],
            mode=payload["mode"],
            available_credits=payload["available_credits"],
            overage_enabled=payload["overage_enabled"],
            quota_ids=payload["quota_ids"],
            reason=payload["reason"],
        )

    async def AuthorizeUsage(self, request, context):
        decision = await authorize_usage(_usage_event_from_proto(request.event))
        return billing_pb2.AuthorizeUsageResponse(
            allowed=decision.allowed,
            mode=decision.mode,
            prepaid_value=decision.prepaid_value,
            overage_value=decision.overage_value,
            allocations=[
                billing_pb2.UsageAllocation(quota_id=item["quota_id"], value=item["value"])
                for item in decision.allocations
            ],
            reason=decision.reason,
        )

    async def RecordUsageSync(self, request, context):
        try:
            decision = await record_usage(_usage_event_from_proto(request.event))
        except ValueError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except Exception as exc:  # pragma: no cover - external failure path
            logger.error("Billing RecordUsageSync failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

        return billing_pb2.RecordUsageSyncResponse(
            status="recorded" if decision.allowed else "blocked",
            mode=decision.mode,
            prepaid_value=decision.prepaid_value,
            overage_value=decision.overage_value,
            allocations=[
                billing_pb2.UsageAllocation(quota_id=item["quota_id"], value=item["value"])
                for item in decision.allocations
            ],
            stripe_meter_event_id=decision.stripe_meter_event_id,
        )


def _usage_event_from_proto(event: billing_pb2.BillingUsageEvent) -> dict[str, Any]:
    metadata = (
        MessageToDict(event.metadata, preserving_proto_field_name=True)
        if event.HasField("metadata")
        else {}
    )
    occurred_at = _timestamp_to_datetime(event.occurred_at) if event.HasField("occurred_at") else None
    return {
        "version": event.version,
        "source_service": event.source_service,
        "workspace_id": event.workspace_id,
        "event_type": event.event_type,
        "value": event.value,
        "idempotency_key": event.idempotency_key,
        "occurred_at": occurred_at,
        "metadata": metadata,
    }


def _subscription_to_proto(subscription: dict[str, Any]) -> billing_pb2.SubscriptionSnapshot:
    return billing_pb2.SubscriptionSnapshot(
        subscription_id=subscription["subscription_id"],
        workspace_id=subscription["workspace_id"],
        status=subscription["status"],
        plan_name=subscription["plan_name"],
        product_id=subscription["product_id"],
        price_id=subscription["price_id"],
        price_amount=int(subscription["price_amount"]),
        currency=subscription["currency"],
        interval=subscription["interval"],
        current_period_start=_datetime_to_timestamp(subscription.get("current_period_start")),
        current_period_end=_datetime_to_timestamp(subscription.get("current_period_end")),
        cancel_at_period_end=subscription.get("cancel_at_period_end", False),
        cancel_at=_datetime_to_timestamp(subscription.get("cancel_at")),
        trial_end=_datetime_to_timestamp(subscription.get("trial_end")),
    )


def _usage_snapshot_to_proto(usage: dict[str, Any]) -> billing_pb2.UsageSnapshot:
    return billing_pb2.UsageSnapshot(
        total_allocated=float(usage.get("total_allocated") or 0),
        total_used=float(usage.get("total_used") or 0),
        overage=float(usage.get("overage") or 0),
        usage_percentage=float(usage.get("usage_percentage") or 0),
    )


def _invoice_to_proto(invoice: dict[str, Any]) -> billing_pb2.InvoiceSnapshot:
    return billing_pb2.InvoiceSnapshot(
        invoice_id=invoice["invoice_id"],
        number=invoice.get("number") or "",
        status=invoice.get("status") or "",
        amount_due=int(invoice.get("amount_due") or 0),
        amount_paid=int(invoice.get("amount_paid") or 0),
        currency=invoice.get("currency") or "",
        description=invoice.get("description") or "",
        created_at=_datetime_to_timestamp(invoice.get("created_at")),
        due_date=_datetime_to_timestamp(invoice.get("due_date")),
        invoice_pdf=invoice.get("invoice_pdf") or "",
        hosted_invoice_url=invoice.get("hosted_invoice_url") or "",
        subscription_id=invoice.get("subscription_id") or "",
        billing_reason=invoice.get("billing_reason") or "",
    )


def _plan_to_proto(plan: dict[str, Any]) -> billing_pb2.PlanEntry:
    metadata = Struct()
    metadata.update(plan.get("metadata") or {})
    return billing_pb2.PlanEntry(
        product_id=plan["product_id"],
        name=plan["name"],
        description=plan.get("description") or "",
        tier=plan.get("tier") or "",
        features=plan.get("features") or [],
        metadata=metadata,
        price=billing_pb2.PriceEntry(
            price_id=plan["price"]["price_id"],
            unit_amount=int(plan["price"]["unit_amount"]),
            currency=plan["price"]["currency"],
            interval=plan["price"].get("interval") or "",
            interval_count=int(plan["price"].get("interval_count") or 0),
            type=plan["price"].get("type") or "",
        ),
    )


def _datetime_to_timestamp(value: datetime | None) -> Timestamp:
    timestamp = Timestamp()
    if not value:
        return timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    timestamp.FromDatetime(value.astimezone(timezone.utc))
    return timestamp


def _timestamp_to_datetime(value: Timestamp) -> datetime:
    return value.ToDatetime().replace(tzinfo=timezone.utc)

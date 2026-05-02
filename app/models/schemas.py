"""Pydantic schemas for API requests, responses, and internal data models."""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field

from app.models.enums import (
    PlanTier,
    SubscriptionStatus,
    UsageEventType,
    PaymentGateway,
)


# ─── Entitlement Schemas ───


class EntitlementResponse(BaseModel):
    """Response for entitlement checks."""
    workspace_id: str
    has_active_subscription: bool
    plan_tier: Optional[PlanTier] = None
    subscription_status: Optional[SubscriptionStatus] = None
    features: list[str] = Field(default_factory=list)
    usage: dict[str, "UsageSummary"] = Field(default_factory=dict)
    is_quota_exceeded: bool = False
    payment_overdue: bool = False
    stripe_customer_id: Optional[str] = None
    cached: bool = False
    cached_at: Optional[datetime] = None


class UsageSummary(BaseModel):
    """Usage summary for a single meter."""
    used: float = 0.0
    limit: Optional[float] = None
    percentage: Optional[float] = None


class UsageEventRequest(BaseModel):
    """A usage event to be metered."""
    workspace_id: str
    event_type: UsageEventType
    value: float
    idempotency_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class UsageReportResponse(BaseModel):
    """Usage report for a workspace."""
    workspace_id: str
    period_start: datetime
    period_end: datetime
    meters: dict[str, UsageSummary] = Field(default_factory=dict)


class UsageByDayResponse(BaseModel):
    """Usage broken down by day and category."""
    workspace_id: str
    start_date: str
    end_date: str
    data: list[dict[str, Any]] = Field(default_factory=list)


# ─── SQS Message Schemas ───


class SQSUsageEvent(BaseModel):
    """Usage event message consumed from SQS."""
    event_type: UsageEventType
    workspace_id: str
    value: float
    idempotency_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SQSStripeEvent(BaseModel):
    """Stripe webhook event forwarded via SQS."""
    event_id: str
    event_type: str  # e.g. "invoice.paid", "customer.subscription.updated"
    data: dict[str, Any]
    created: int  # Unix timestamp


class SQSPaymentEvent(BaseModel):
    """Payment event from external gateways (Razorpay, Zoho, manual)."""
    source: PaymentGateway
    event_type: str  # e.g. "payment.captured", "manual_reconciliation"
    workspace_id: str
    amount: float
    currency: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─── Health Check ───


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str
    redis: bool
    database: bool
    stripe: bool

"""Pydantic schemas for API requests, responses, and internal data models."""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field

from app.models.enums import (
    PlanTier,
    SubscriptionStatus,
    UsageEventType,
    PaymentGateway,
    InvoiceStatus,
    PaymentCollectionMethod,
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


class FeatureCheckResponse(BaseModel):
    """Response for a single feature access check."""
    workspace_id: str
    feature: str
    has_access: bool
    reason: Optional[str] = None


# ─── Subscription Schemas ───


class SubscriptionResponse(BaseModel):
    """Subscription details response."""
    subscription_id: str
    workspace_id: str
    stripe_customer_id: str
    plan_tier: PlanTier
    status: SubscriptionStatus
    collection_method: PaymentCollectionMethod
    preferred_gateway: PaymentGateway = PaymentGateway.STRIPE
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool = False
    trial_end: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateSubscriptionRequest(BaseModel):
    """Request to create a new subscription."""
    workspace_id: str
    user_id: str
    email: str
    plan_price_id: str
    collection_method: PaymentCollectionMethod = PaymentCollectionMethod.CHARGE_AUTOMATICALLY
    preferred_gateway: PaymentGateway = PaymentGateway.STRIPE
    trial_days: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelSubscriptionRequest(BaseModel):
    """Request to cancel a subscription."""
    workspace_id: str
    cancel_immediately: bool = False


class ChangeSubscriptionRequest(BaseModel):
    """Request to change subscription plan."""
    workspace_id: str
    new_price_id: str
    proration_behavior: str = "create_prorations"  # or "none", "always_invoice"


# ─── Usage Schemas ───


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


# ─── Checkout Schemas ───


class CreateCheckoutRequest(BaseModel):
    """Request to create a checkout session."""
    workspace_id: str
    user_id: str
    email: str
    price_id: str
    success_url: str
    cancel_url: str
    gateway: PaymentGateway = PaymentGateway.STRIPE
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckoutResponse(BaseModel):
    """Checkout session response."""
    checkout_url: str
    session_id: str
    gateway: PaymentGateway


class CreateRazorpayPaymentRequest(BaseModel):
    """Request to create a Razorpay payment for an existing Stripe invoice."""
    workspace_id: str
    stripe_invoice_id: str


class RazorpayPaymentResponse(BaseModel):
    """Razorpay order details for frontend checkout."""
    razorpay_order_id: str
    amount: int  # in paise
    currency: str
    stripe_invoice_id: str


# ─── Invoice Schemas ───


class InvoiceResponse(BaseModel):
    """Invoice details response."""
    invoice_id: str
    workspace_id: str
    status: InvoiceStatus
    amount_due: int
    amount_paid: int
    currency: str
    due_date: Optional[datetime] = None
    pdf_url: Optional[str] = None
    hosted_url: Optional[str] = None
    created_at: datetime


class SendInvoiceRequest(BaseModel):
    """Request to send an invoice to a customer."""
    channel: str = "email"  # "email" or "whatsapp"
    phone_number: Optional[str] = None


class ReconcilePaymentRequest(BaseModel):
    """Request to manually reconcile a payment against a Stripe invoice."""
    workspace_id: str
    stripe_invoice_id: str
    amount: float
    currency: str = "INR"
    transfer_method: str  # "neft", "rtgs", "upi", "cheque"
    bank_reference: str  # UTR number / transaction reference
    transfer_date: datetime
    notes: Optional[str] = None


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

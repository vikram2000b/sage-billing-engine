"""Enums used across the billing engine."""

from enum import Enum


class PlanTier(str, Enum):
    """Subscription plan tiers."""
    FREE = "free"
    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class SubscriptionStatus(str, Enum):
    """Stripe subscription statuses."""
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    UNPAID = "unpaid"
    PAUSED = "paused"


class UsageEventType(str, Enum):
    """Types of usage events that get metered."""
    AI_CREDITS = "ai_credits"
    WHATSAPP_MESSAGE = "whatsapp_message"
    EMAIL_SEND = "email_send"
    SMS_SEND = "sms_send"


class PaymentGateway(str, Enum):
    """Supported payment gateways."""
    STRIPE = "stripe"
    RAZORPAY = "razorpay"
    MANUAL_BANK_TRANSFER = "manual_bank_transfer"
    ZOHO_BOOKS = "zoho_books"


class InvoiceStatus(str, Enum):
    """Stripe invoice statuses."""
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class PaymentCollectionMethod(str, Enum):
    """How payments are collected for a subscription."""
    CHARGE_AUTOMATICALLY = "charge_automatically"
    SEND_INVOICE = "send_invoice"


class UsageEventStatus(str, Enum):
    """Status of a usage event in the pipeline."""
    PENDING = "pending"
    SENT = "sent"
    CONFIRMED = "confirmed"
    FAILED = "failed"

# Sage Billing Engine

Centralized billing service for the Sage platform. Handles Stripe subscriptions, usage-based metering, entitlement checks, multi-gateway payment collection (Stripe, Razorpay, manual bank transfers), and event-driven reconciliation.

**Architecture**: gRPC server + SQS consumers running in a single process.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Sage Billing Engine                           │
│                                                                     │
│  ┌──────────────────────────┐   ┌────────────────────────────────┐  │
│  │      gRPC Server         │   │       SQS Consumers            │  │
│  │                          │   │                                │  │
│  │  BillingService          │   │  usage-events   → Stripe Meter │  │
│  │  entitlement checks      │   │  stripe-events  → Projection   │  │
│  │  plan/invoice/session    │   │  payment-events → Reconcile    │  │
│  │  usage authorization     │   │                                │  │
│  └──────────────────────────┘   └────────────────────────────────┘  │
└──────────┬──────────────────────────────┬───────────────────────────┘
           │                              │
     ┌─────▼─────┐                 ┌──────▼──────┐
     │   Redis    │                 │   Stripe    │
     │  (cache)   │                 │  (source    │
     │            │                 │   of truth) │
     └───────────┘                 └─────────────┘
```

### Design Principles

- **Stripe is the source of truth** for subscriptions, invoices, products, prices, and usage meters. No local tables for plans or subscriptions.
- **Redis is a low-latency cache** for entitlements, feature flags, and real-time usage counters. All cache entries are invalidated by Stripe webhook events.
- **SQS decouples event processing** — usage events, Stripe webhooks, and external payment events are consumed asynchronously.
- **Multi-gateway payments** are reconciled through Stripe's "pay out of band" feature, keeping Stripe as the single billing ledger regardless of how payment was collected.

## Project Structure

```
sage-billing-engine/
├── app/
│   ├── main.py                  # gRPC service entry point
│   ├── grpc/                    # Billing gRPC server + servicers
│   │   ├── server.py            # gRPC bootstrap + SQS consumer lifecycle
│   │   └── billing_servicer.py  # BillingService implementation
│   ├── services/                # Business logic layer
│   │   ├── entitlement_service.py
│   │   ├── subscription_service.py
│   │   ├── usage_service.py
│   │   └── checkout_service.py
│   ├── consumers/               # SQS message handlers
│   │   ├── usage_events.py      # Usage → Stripe Meters + Redis counters
│   │   ├── stripe_events.py     # Stripe webhooks → cache invalidation
│   │   └── payment_events.py    # Razorpay/manual/Zoho → Stripe reconciliation
│   ├── clients/                 # External service wrappers
│   │   ├── stripe_client.py     # Stripe SDK wrapper
│   │   └── sqs_client.py        # AWS SQS publish + consume
│   ├── models/                  # Pydantic schemas & enums
│   │   ├── enums.py             # PlanTier, UsageEventType, PaymentGateway, etc.
│   │   └── schemas.py           # Request/response models
│   └── core/                    # Infrastructure & config
│       ├── config.py            # Settings (env vars, Stripe keys, SQS URLs, Redis)
│       ├── redis.py             # Redis client (caching, counters, health check)
│       ├── cache.py             # aiocache decorator-based caching
│       ├── db.py                # Database connection (gRPC)
│       ├── grpc_clients.py      # gRPC client for DatabaseAccess service
│       └── logging.py           # Structured logging + OpenTelemetry tracing
├── protos/                      # Protobuf definitions
├── sagepilot/                   # Generated gRPC stubs
├── pyproject.toml               # Poetry dependencies & tool config
└── poetry.lock
```

## gRPC Surface

The service exposes a single internal `sagepilot.billing.BillingService` over gRPC.

| Method | Description |
|--------|-------------|
| `GetBillingSummary` | Workspace billing snapshot for platform settings and guards |
| `GetPlans` | Product and price catalog |
| `GetInvoices` | Invoice history |
| `CreatePortalSession` | Stripe customer portal session |
| `CreateCustomerSession` | Stripe pricing table customer session |
| `CheckEntitlement` | Feature gate lookup |
| `CheckUsageEligibility` | Quota / overage decision without mutation |
| `AuthorizeUsage` | Allocation decision for a usage event |
| `RecordUsageSync` | Low-volume synchronous usage recording fallback |

## SQS Consumers

Three SQS consumers run as background `asyncio` tasks alongside the gRPC server:

| Queue | Handler | Purpose |
|-------|---------|---------|
| `billing-usage-events` | `usage_events.py` | Pushes usage to Stripe Meters, updates Redis counters |
| `billing-stripe-events` | `stripe_events.py` | Processes Stripe webhooks — invalidates caches, resets counters on renewal |
| `billing-payment-events` | `payment_events.py` | Reconciles Razorpay, manual, and Zoho payments against Stripe invoices |

## Payment Collection Flows

### Stripe (International)
Standard Stripe Checkout flow. Customer pays via card, and Stripe handles the full lifecycle.

### Razorpay (India — UPI, Netbanking, Cards)
1. Stripe creates the subscription with `collection_method: send_invoice`.
2. When an invoice is due, the platform creates a Razorpay order linked to the Stripe invoice.
3. Customer pays via Razorpay (UPI, netbanking, etc.).
4. Razorpay webhook fires `payment.captured` → SQS consumer marks the Stripe invoice as `paid_out_of_band`.

### Manual Bank Transfer (NEFT/RTGS/Cheque)
1. Stripe creates the invoice with `collection_method: send_invoice`.
2. Invoice is sent to the customer with bank account details.
3. Customer transfers money directly.
4. Finance team calls `POST /checkout/reconcile` with the bank reference → Stripe invoice is marked as `paid_out_of_band`.

## Usage-Based Billing (Stripe Meters)

The following usage types are metered:

| Meter | Event Type | Description |
|-------|------------|-------------|
| `ai_credits` | `AI_CREDITS` | Token-based AI usage (conversations, completions) |
| `whatsapp_messages` | `WHATSAPP_MESSAGE` | WhatsApp messages sent |
| `email_sends` | `EMAIL_SEND` | Marketing emails sent |

Usage events flow: **Platform → SQS → Consumer → Stripe Meter + Redis Counter**

Redis counters provide real-time, low-latency usage checks for entitlement enforcement. Stripe Meters are the authoritative record for invoicing.

## Setup

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/) for dependency management
- Redis (for entitlement cache & usage counters)
- AWS credentials (for SQS access)
- Stripe account with API keys
- Razorpay account (optional, for Indian payments)

### Installation

```bash
cd sage-billing-engine
poetry install
```

### Configuration

Create a `.env` file in the project root:

```env
# Stripe
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_METER_AI_CREDITS=ai_credits
STRIPE_METER_WHATSAPP_MESSAGES=whatsapp_messages
STRIPE_METER_EMAIL_SENDS=email_sends

# Razorpay (optional)
RAZORPAY_KEY_ID=rzp_live_...
RAZORPAY_KEY_SECRET=...

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0
REDIS_CLUSTER_MODE=false

# SQS Queue URLs
SQS_USAGE_EVENTS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789/billing-usage-events
SQS_STRIPE_EVENTS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789/billing-stripe-events
SQS_PAYMENT_EVENTS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789/billing-payment-events

# gRPC
DATABASE_ACCESS_GRPC_HOST=localhost
DATABASE_ACCESS_GRPC_PORT=50051

# Observability
LOG_LEVEL=INFO
ENABLE_OTEL_TRACING=false
```

### Running

```bash
poetry run python -m app.main
```

The process starts:
- the internal gRPC server on `BILLING_GRPC_HOST:BILLING_GRPC_PORT`
- Redis and database clients
- any configured SQS consumers

## Integration Guide

### Checking Entitlements (from other services)

```python
# Use the internal BillingService gRPC client from platform-api or another trusted service.
# The proto lives under protos/sagepilot/billing/billing.proto.
```

### Publishing Usage Events (from other services)

```python
# Preferred: publish the usage event to the billing usage SQS queue.
# Fallback: call RecordUsageSync over gRPC for low-volume synchronous flows.
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | gRPC (grpc.aio) |
| Billing | Stripe (subscriptions, meters, invoices) |
| Indian Payments | Razorpay |
| Queue | AWS SQS |
| Cache | Redis |
| Function Cache | aiocache |
| Database Access | gRPC (DatabaseAccess service) |
| Observability | OpenTelemetry → SigNoz |
| Validation | Pydantic v2 |
| HTTP Client | httpx |

# Sage Billing Engine

Centralized billing service for the Sage platform. Handles Stripe subscriptions, usage-based metering, entitlement checks, multi-gateway payment collection (Stripe, Razorpay, manual bank transfers), and event-driven reconciliation.

**Architecture**: FastAPI server + SQS consumers running in a single process.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Sage Billing Engine                           │
│                                                                     │
│  ┌──────────────────────────┐   ┌────────────────────────────────┐  │
│  │     FastAPI Server       │   │       SQS Consumers            │  │
│  │                          │   │                                │  │
│  │  /api/v1/entitlements    │   │  usage-events   → Stripe Meter │  │
│  │  /api/v1/subscriptions   │   │  stripe-events  → Cache Sync  │  │
│  │  /api/v1/usage           │   │  payment-events → Reconcile   │  │
│  │  /api/v1/checkout        │   │                                │  │
│  │  /api/v1/plans           │   └────────────────────────────────┘  │
│  │  /api/v1/webhooks        │                                       │
│  └──────────────────────────┘                                       │
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
│   ├── main.py                  # FastAPI app + SQS consumer startup (lifespan)
│   ├── api/                     # FastAPI route handlers
│   │   ├── __init__.py          # Router aggregation (all routes under /api/v1)
│   │   ├── entitlements.py      # Feature access & usage limit checks
│   │   ├── subscriptions.py     # Subscription CRUD, plan changes, pause/resume
│   │   ├── usage.py             # Usage event recording (sync & async)
│   │   ├── checkout.py          # Checkout sessions, invoices, reconciliation
│   │   ├── plans.py             # Product catalog from Stripe
│   │   └── webhooks.py          # Stripe & Razorpay webhook receivers
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

## API Reference

All routes are prefixed with `/api/v1`.

### Entitlements

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/entitlements/{workspace_id}` | Full entitlements (plan, features, usage, limits) |
| `GET` | `/entitlements/{workspace_id}/feature/{feature}` | Check access to a specific feature |
| `GET` | `/entitlements/{workspace_id}/usage/{meter}/exceeded` | Check if usage limit is exceeded |
| `POST` | `/entitlements/{workspace_id}/invalidate` | Force-invalidate the entitlement cache |

### Subscriptions

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/subscriptions/{workspace_id}` | Get current subscription |
| `POST` | `/subscriptions/` | Create a new subscription |
| `POST` | `/subscriptions/cancel` | Cancel a subscription |
| `POST` | `/subscriptions/{workspace_id}/change-plan` | Upgrade or downgrade plan |
| `POST` | `/subscriptions/{workspace_id}/pause` | Pause a subscription |
| `POST` | `/subscriptions/{workspace_id}/resume` | Resume a paused subscription |
| `POST` | `/subscriptions/{workspace_id}/revoke-cancellation` | Revoke a pending cancellation |

### Usage

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/usage/events` | Record a usage event synchronously (Stripe Meter + Redis) |
| `POST` | `/usage/events/async` | Publish a usage event to SQS for async processing |
| `GET` | `/usage/{workspace_id}` | Get usage report for a billing period |

### Checkout & Invoices

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/checkout/session` | Create a Stripe Checkout session |
| `POST` | `/checkout/razorpay` | Create a Razorpay order for a Stripe invoice |
| `GET` | `/checkout/invoices/{workspace_id}` | List invoices for a workspace |
| `GET` | `/checkout/invoices/detail/{invoice_id}` | Get invoice details |
| `POST` | `/checkout/invoices/{invoice_id}/send` | Send an invoice via email/WhatsApp |
| `POST` | `/checkout/reconcile` | Manually reconcile a bank transfer against a Stripe invoice |

### Plans

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/plans/` | List all available plans from Stripe (cached 10 min) |

### Webhooks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/webhooks/stripe` | Stripe webhook receiver (signature-verified) |
| `POST` | `/webhooks/razorpay` | Razorpay webhook receiver |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Service health check (includes Redis status) |

## SQS Consumers

Three SQS consumers run as background `asyncio` tasks alongside the FastAPI server:

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
# Development (with hot reload)
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Production
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **Note**: Use `--workers 1` in production. SQS consumers run as `asyncio` background tasks within the FastAPI process, so multiple workers would spawn duplicate consumers.

API docs are available at `http://localhost:8000/docs` (Swagger UI) and `http://localhost:8000/redoc` (ReDoc).

## Integration Guide

### Checking Entitlements (from other services)

```python
# Quick feature gate
response = await httpx.get(
    "http://billing-engine:8000/api/v1/entitlements/{workspace_id}/feature/whatsapp_campaigns"
)
has_access = response.json()["has_access"]

# Full entitlements (plan, features, usage, limits)
response = await httpx.get(
    "http://billing-engine:8000/api/v1/entitlements/{workspace_id}"
)
entitlements = response.json()
```

### Publishing Usage Events (from other services)

```python
# Async (preferred for high-throughput — AI messages, WhatsApp, email)
await httpx.post("http://billing-engine:8000/api/v1/usage/events/async", json={
    "event_type": "ai_credits",
    "workspace_id": "ws_abc123",
    "value": 2.5,
    "idempotency_key": "msg_xyz789",
})

# Sync (immediate Stripe push — for low-volume or critical events)
await httpx.post("http://billing-engine:8000/api/v1/usage/events", json={
    "event_type": "whatsapp_message",
    "workspace_id": "ws_abc123",
    "value": 1,
    "idempotency_key": "wa_msg_456",
})
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI |
| Billing | Stripe (subscriptions, meters, invoices) |
| Indian Payments | Razorpay |
| Queue | AWS SQS |
| Cache | Redis |
| Function Cache | aiocache |
| Database Access | gRPC (DatabaseAccess service) |
| Observability | OpenTelemetry → SigNoz |
| Validation | Pydantic v2 |
| HTTP Client | httpx |

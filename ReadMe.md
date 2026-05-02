# Sage Billing Engine

Billing-engine is the only service in this workspace that is allowed to talk to Stripe directly.

Current launch shape:
- `agi-admin-portal` calls `sage-platform-api`
- `sage-platform-api` calls billing-engine gRPC
- billing-engine owns Stripe reads/writes, billing projections, entitlement checks, usage allocation, and async usage enqueueing

## Runtime Model

Billing-engine runs one process with:
- a gRPC server
- a Stripe event consumer
- a usage event consumer
- a payment reconciliation consumer

Core files:
- [server.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/grpc/server.py)
- [billing_servicer.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/grpc/billing_servicer.py)
- [billing_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_service.py)
- [billing_projection_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_projection_service.py)
- [billing_usage_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_usage_service.py)
- [entitlement_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/entitlement_service.py)
- [usage_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/usage_service.py)
- [billing_repository.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/repositories/billing_repository.py)
- [stripe_client.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/clients/stripe_client.py)

## Source of Truth

The split is now intentional:
- Stripe is the external commercial system of record.
- billing-engine projections are the application-facing source of truth.

That means:
- subscriptions are projected into `stripe_subscriptions`
- customer mappings are stored in `stripe_customers`
- usage/quota state is stored in `workspace_quotas`, `quota_transactions`, and `stripe_usage_records`
- entitlement answers are derived from billing-engine state, with Stripe product metadata hydrated only when needed for features and plan limits

## gRPC Surface

The active gRPC surface is defined in [billing.proto](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/protos/sagepilot/billing/billing.proto).

Current RPCs:
- `GetBillingSummary`
- `GetPlans`
- `GetInvoices`
- `CreatePortalSession`
- `CreateCustomerSession`
- `CheckEntitlement`
- `CheckUsageEligibility`
- `AuthorizeUsage`
- `RecordUsageSync`
- `RecordUsageAsync`

Notes:
- `RecordUsageAsync` publishes to SQS through billing-engine.
- `RecordUsageSync` is the synchronous fallback path.
- There is no active checkout/subscription lifecycle RPC surface in this repo right now.

## Event Flows

Stripe subscription and invoice events:
- Stripe/EventBridge -> `stripe-events` SQS -> [billing_projection_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_projection_service.py)
- billing-engine updates projections and invalidates entitlement cache

Usage events:
- app -> platform-api -> billing-engine gRPC
- either:
  - `RecordUsageSync` -> immediate allocation/metering
  - `RecordUsageAsync` -> publish to usage SQS
- usage SQS consumer -> [billing_usage_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_usage_service.py)

Payment reconciliation:
- external payment event -> payment SQS -> [payment_events.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/consumers/payment_events.py)
- billing-engine marks Stripe invoices paid out-of-band where applicable

## Entitlements

Entitlements are served by [entitlement_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/entitlement_service.py).

Current behavior:
- Redis caches entitlement payloads
- billing-engine projections decide whether the workspace has an active subscription
- Stripe product metadata provides plan tier, feature list, and configured usage limits
- Redis usage counters provide low-latency usage-limit checks

## Usage and Metering

Usage allocation and metering are owned by [billing_usage_service.py](/Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine/app/services/billing_usage_service.py).

Current supported billable event types:
- `ai_credits`
- `whatsapp_message`
- `email_send`

What it does:
- checks active allocatable quotas
- consumes prepaid credits first
- meters residual overage to Stripe when configured
- writes usage audit rows
- updates Redis counters for fast eligibility checks

## Configuration

Important env vars:
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_METER_AI_CREDITS`
- `STRIPE_METER_WHATSAPP_MESSAGES`
- `STRIPE_METER_EMAIL_SENDS`
- `SQS_USAGE_EVENTS_QUEUE_URL`
- `SQS_STRIPE_EVENTS_QUEUE_URL`
- `SQS_PAYMENT_EVENTS_QUEUE_URL`
- `DATABASE_ACCESS_GRPC_HOST`
- `DATABASE_ACCESS_GRPC_PORT`

If `SQS_USAGE_EVENTS_QUEUE_URL` is unset, `RecordUsageAsync` now fails explicitly instead of silently pretending to queue.

## Running

```bash
cd /Users/vikram/Documents/Sagepilot/sage-marketing-workspace/sage-billing-engine
poetry run python -m app.main
```

## Launch Boundary

For the current India/EU rollout:
- each deployment should use one Stripe account
- platform-api must not call Stripe directly
- agi-admin-portal must not call Stripe directly from server helpers in this workspace
- billing-engine is the only Stripe-facing backend

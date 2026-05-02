"""Persistence helpers for billing projections and usage allocation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.db import Database


ALLOCATION_STATUSES = """
  (
    status IN ('active', 'trialing')
    OR (status = 'past_due' AND quota_start_date + INTERVAL '3 days' > NOW())
  )
"""


class BillingRepository:
    """Repository for billing-owned projection tables."""

    async def get_customer_mapping(self, workspace_id: str) -> dict[str, Any] | None:
        rows = await Database.execute_query(
            """
            SELECT workspace_id, stripe_customer_id, stripe_subscription_id, billing_type
            FROM stripe_customers
            WHERE workspace_id = $1
            LIMIT 1
            """,
            workspace_id,
            use_replica=True,
        )
        return dict(rows[0]) if rows else None

    async def get_workspace_by_customer(self, stripe_customer_id: str) -> str | None:
        rows = await Database.execute_query(
            """
            SELECT workspace_id
            FROM stripe_customers
            WHERE stripe_customer_id = $1
            LIMIT 1
            """,
            stripe_customer_id,
            use_replica=True,
        )
        return str(rows[0]["workspace_id"]) if rows else None

    async def upsert_customer_mapping(
        self,
        workspace_id: str,
        stripe_customer_id: str,
        stripe_subscription_id: str | None = None,
        billing_type: str = "subscription",
    ) -> None:
        await Database.execute_query(
            """
            INSERT INTO stripe_customers (
                workspace_id,
                stripe_customer_id,
                stripe_subscription_id,
                billing_type,
                updated_at
            )
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (workspace_id) DO UPDATE SET
                stripe_customer_id = EXCLUDED.stripe_customer_id,
                stripe_subscription_id = COALESCE(
                    EXCLUDED.stripe_subscription_id,
                    stripe_customers.stripe_subscription_id
                ),
                billing_type = EXCLUDED.billing_type,
                updated_at = NOW()
            """,
            workspace_id,
            stripe_customer_id,
            stripe_subscription_id,
            billing_type,
            fetch=False,
        )

    async def set_workspace_billing_provider(
        self,
        workspace_id: str,
        provider: str = "stripe",
    ) -> None:
        await Database.execute_query(
            """
            UPDATE workspaces
            SET billing_provider = $1
            WHERE id = $2
            """,
            provider,
            workspace_id,
            fetch=False,
        )

    async def mark_other_subscriptions_canceled(
        self,
        workspace_id: str,
        current_subscription_id: str,
    ) -> None:
        await Database.execute_query(
            """
            UPDATE stripe_subscriptions
            SET status = 'canceled', canceled_at = NOW(), updated_at = NOW()
            WHERE workspace_id = $1
              AND status IN ('active', 'trialing')
              AND id != $2
            """,
            workspace_id,
            current_subscription_id,
            fetch=False,
        )

    async def upsert_subscription_projection(self, payload: dict[str, Any]) -> None:
        metadata_json = json.dumps(payload.get("metadata") or {}, default=str)
        await Database.execute_query(
            """
            INSERT INTO stripe_subscriptions (
                id,
                workspace_id,
                user_id,
                stripe_customer_id,
                stripe_product_id,
                stripe_price_id,
                status,
                billing_interval,
                currency,
                current_period_start,
                current_period_end,
                cancel_at_period_end,
                cancel_at,
                canceled_at,
                trial_end,
                metadata,
                created_at,
                updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10, $11, $12, $13, $14, $15, $16, NOW(), NOW()
            )
            ON CONFLICT (id) DO UPDATE SET
                workspace_id = EXCLUDED.workspace_id,
                user_id = EXCLUDED.user_id,
                stripe_customer_id = EXCLUDED.stripe_customer_id,
                stripe_product_id = EXCLUDED.stripe_product_id,
                stripe_price_id = EXCLUDED.stripe_price_id,
                status = EXCLUDED.status,
                billing_interval = EXCLUDED.billing_interval,
                currency = EXCLUDED.currency,
                current_period_start = EXCLUDED.current_period_start,
                current_period_end = EXCLUDED.current_period_end,
                cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                cancel_at = EXCLUDED.cancel_at,
                canceled_at = EXCLUDED.canceled_at,
                trial_end = EXCLUDED.trial_end,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            payload["id"],
            payload["workspace_id"],
            payload.get("user_id"),
            payload["stripe_customer_id"],
            payload.get("stripe_product_id"),
            payload.get("stripe_price_id"),
            payload["status"],
            payload["billing_interval"],
            payload["currency"],
            payload["current_period_start"],
            payload["current_period_end"],
            payload.get("cancel_at_period_end", False),
            payload.get("cancel_at"),
            payload.get("canceled_at"),
            payload.get("trial_end"),
            metadata_json,
            fetch=False,
        )

    async def get_subscription_projection(self, workspace_id: str) -> dict[str, Any] | None:
        rows = await Database.execute_query(
            """
            SELECT
                id,
                workspace_id,
                stripe_customer_id,
                stripe_product_id,
                stripe_price_id,
                status,
                billing_interval,
                currency,
                current_period_start,
                current_period_end,
                cancel_at_period_end,
                cancel_at,
                trial_end
            FROM stripe_subscriptions
            WHERE workspace_id = $1
            ORDER BY
              CASE status
                WHEN 'active' THEN 1
                WHEN 'trialing' THEN 2
                WHEN 'past_due' THEN 3
                WHEN 'canceled' THEN 4
                ELSE 5
              END,
              created_at DESC
            LIMIT 1
            """,
            workspace_id,
            use_replica=True,
        )
        return dict(rows[0]) if rows else None

    async def get_subscription_projection_by_id(
        self,
        subscription_id: str,
    ) -> dict[str, Any] | None:
        rows = await Database.execute_query(
            """
            SELECT *
            FROM stripe_subscriptions
            WHERE id = $1
            LIMIT 1
            """,
            subscription_id,
            use_replica=True,
        )
        return dict(rows[0]) if rows else None

    async def update_subscription_projection(
        self,
        subscription_id: str,
        *,
        status: str,
        current_period_start: datetime | None = None,
        current_period_end: datetime | None = None,
        cancel_at_period_end: bool | None = None,
        cancel_at: datetime | None = None,
        canceled_at: datetime | None = None,
    ) -> None:
        await Database.execute_query(
            """
            UPDATE stripe_subscriptions
            SET status = $1,
                current_period_start = COALESCE($2, current_period_start),
                current_period_end = COALESCE($3, current_period_end),
                cancel_at_period_end = COALESCE($4, cancel_at_period_end),
                cancel_at = COALESCE($5, cancel_at),
                canceled_at = COALESCE($6, canceled_at),
                updated_at = NOW()
            WHERE id = $7
            """,
            status,
            current_period_start,
            current_period_end,
            cancel_at_period_end,
            cancel_at,
            canceled_at,
            subscription_id,
            fetch=False,
        )

    async def create_quota(self, payload: dict[str, Any]) -> None:
        await Database.execute_query(
            """
            INSERT INTO workspace_quotas (
                workspace_id,
                subscription_id,
                total_credits,
                used_credits,
                status,
                quota_start_date,
                quota_end_date,
                start_date,
                end_date,
                priority,
                updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $6, $7, $8, NOW())
            """,
            payload["workspace_id"],
            payload["subscription_id"],
            payload["total_credits"],
            payload.get("used_credits", 0),
            payload["status"],
            payload["quota_start_date"],
            payload["quota_end_date"],
            payload["priority"],
            fetch=False,
        )

    async def expire_active_quotas(self, subscription_id: str) -> None:
        await Database.execute_query(
            """
            UPDATE workspace_quotas
            SET status = 'expired', updated_at = NOW()
            WHERE subscription_id = $1
              AND status IN ('active', 'trialing', 'past_due')
            """,
            subscription_id,
            fetch=False,
        )

    async def expire_workspace_free_quotas(self, workspace_id: str) -> None:
        await Database.execute_query(
            """
            UPDATE workspace_quotas
            SET status = 'expired', updated_at = NOW()
            WHERE workspace_id = $1
              AND subscription_id LIKE 'free_%'
              AND status IN ('active', 'trialing', 'past_due')
            """,
            workspace_id,
            fetch=False,
        )

    async def update_quota_status(self, subscription_id: str, status: str) -> None:
        await Database.execute_query(
            """
            UPDATE workspace_quotas
            SET status = $1, updated_at = NOW()
            WHERE subscription_id = $2
              AND status NOT IN ('expired', 'canceled')
            """,
            status,
            subscription_id,
            fetch=False,
        )

    async def quota_exists_for_period(
        self,
        subscription_id: str,
        quota_start_date: datetime,
    ) -> bool:
        rows = await Database.execute_query(
            """
            SELECT id
            FROM workspace_quotas
            WHERE subscription_id = $1
              AND quota_start_date = $2
            LIMIT 1
            """,
            subscription_id,
            quota_start_date,
            use_replica=True,
        )
        return bool(rows)

    async def get_usage_snapshot(self, workspace_id: str) -> dict[str, float]:
        rows = await Database.execute_query(
            f"""
            SELECT
                COALESCE(SUM(total_credits), 0) AS total_allocated,
                COALESCE(SUM(used_credits), 0) AS total_used
            FROM workspace_quotas
            WHERE workspace_id = $1
              AND status IN ('active', 'trialing', 'past_due')
              AND (quota_end_date IS NULL OR quota_end_date >= NOW())
              AND (quota_start_date IS NULL OR quota_start_date <= NOW())
            """,
            workspace_id,
            use_replica=True,
        )
        row = dict(rows[0]) if rows else {"total_allocated": 0, "total_used": 0}
        return {
            "total_allocated": float(row.get("total_allocated") or 0),
            "total_used": float(row.get("total_used") or 0),
        }

    async def list_allocatable_quotas(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = await Database.execute_query(
            f"""
            SELECT
                id,
                subscription_id,
                total_credits,
                used_credits,
                status,
                priority,
                quota_start_date,
                quota_end_date
            FROM workspace_quotas
            WHERE workspace_id = $1
              AND {ALLOCATION_STATUSES}
              AND (quota_end_date IS NULL OR quota_end_date >= NOW())
              AND (quota_start_date IS NULL OR quota_start_date <= NOW())
            ORDER BY priority ASC, quota_start_date ASC NULLS LAST, start_date ASC
            """,
            workspace_id,
            use_replica=False,
        )
        return [dict(row) for row in (rows or [])]

    async def increment_quota_usage(self, quota_id: str, credits: float) -> None:
        await Database.execute_query(
            """
            UPDATE workspace_quotas
            SET used_credits = used_credits + $1, updated_at = NOW()
            WHERE id = $2
            """,
            credits,
            quota_id,
            fetch=False,
        )

    async def insert_quota_transaction(
        self,
        quota_id: str,
        credits: float,
        metadata: dict[str, Any],
    ) -> None:
        await Database.execute_query(
            """
            INSERT INTO quota_transactions (
                quota_id,
                credits_consumed,
                message_id,
                model_id,
                pilot_id,
                action_id,
                actions_taken_id,
                created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
            quota_id,
            credits,
            metadata.get("message_id"),
            metadata.get("model_id"),
            metadata.get("pilot_id"),
            metadata.get("action_id"),
            metadata.get("actions_taken_id"),
            fetch=False,
        )

    async def insert_usage_audit(
        self,
        *,
        workspace_id: str,
        stripe_subscription_item_id: str | None = None,
        meter_type: str,
        quantity: float,
        stripe_usage_record_id: str | None = None,
    ) -> None:
        await Database.execute_query(
            """
            INSERT INTO stripe_usage_records (
                workspace_id,
                stripe_subscription_item_id,
                stripe_usage_record_id,
                meter_type,
                quantity,
                period_start,
                period_end,
                created_at
            ) VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NOW())
            """,
            workspace_id,
            stripe_subscription_item_id,
            stripe_usage_record_id,
            meter_type,
            quantity,
            fetch=False,
        )

    async def is_webhook_processed(self, event_id: str) -> bool:
        rows = await Database.execute_query(
            """
            SELECT event_id
            FROM stripe_webhook_events
            WHERE event_id = $1
            LIMIT 1
            """,
            event_id,
            use_replica=True,
        )
        return bool(rows)

    async def mark_webhook_processed(
        self,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        payload_json = json.dumps(payload, default=str)
        await Database.execute_query(
            """
            INSERT INTO stripe_webhook_events (event_id, event_type, payload, processed_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (event_id) DO NOTHING
            """,
            event_id,
            event_type,
            payload_json,
            fetch=False,
        )


billing_repository = BillingRepository()

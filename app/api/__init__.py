# app/api/__init__.py
"""API router aggregation — includes all route modules."""

from fastapi import APIRouter

from app.api.entitlements import router as entitlements_router
from app.api.subscriptions import router as subscriptions_router
from app.api.usage import router as usage_router
from app.api.checkout import router as checkout_router
from app.api.webhooks import router as webhooks_router
from app.api.plans import router as plans_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(entitlements_router)
api_router.include_router(subscriptions_router)
api_router.include_router(usage_router)
api_router.include_router(checkout_router)
api_router.include_router(webhooks_router)
api_router.include_router(plans_router)

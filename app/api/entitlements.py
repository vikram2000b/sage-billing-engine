"""Entitlement API routes.

Used by other platform services to check if a workspace has access
to features, whether usage limits are exceeded, etc.
"""

from fastapi import APIRouter, Query, HTTPException

from app.services import entitlement_service
from app.models.schemas import EntitlementResponse, FeatureCheckResponse

router = APIRouter(prefix="/entitlements", tags=["Entitlements"])


@router.get("/{workspace_id}", response_model=EntitlementResponse)
async def get_entitlements(
    workspace_id: str,
    refresh: bool = Query(False, description="Bypass cache and fetch from Stripe"),
    ttl: int = Query(None, description="Custom cache TTL in seconds"),
):
    """Get full entitlements for a workspace.

    Returns subscription status, plan tier, available features,
    usage summaries, and quota exceeded flags.
    """
    try:
        return await entitlement_service.get_entitlements(
            workspace_id=workspace_id,
            refresh=refresh,
            ttl=ttl,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{workspace_id}/feature/{feature}", response_model=FeatureCheckResponse)
async def check_feature(workspace_id: str, feature: str):
    """Quick check: does this workspace have access to a specific feature?"""
    try:
        has_access = await entitlement_service.check_feature_access(workspace_id, feature)
        return FeatureCheckResponse(
            workspace_id=workspace_id,
            feature=feature,
            has_access=has_access,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{workspace_id}/usage/{meter}/exceeded")
async def check_usage_exceeded(workspace_id: str, meter: str):
    """Check if a workspace has exceeded its usage limit for a meter."""
    try:
        exceeded = await entitlement_service.check_usage_limit(workspace_id, meter)
        return {"workspace_id": workspace_id, "meter": meter, "exceeded": exceeded}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{workspace_id}/invalidate")
async def invalidate_cache(workspace_id: str):
    """Force-invalidate the entitlement cache for a workspace.

    Useful for debugging or after manual Stripe changes.
    """
    await entitlement_service.invalidate_entitlements(workspace_id)
    return {"status": "invalidated", "workspace_id": workspace_id}

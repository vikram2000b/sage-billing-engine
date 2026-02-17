"""Subscription management API routes."""

from fastapi import APIRouter, HTTPException

from app.services import subscription_service
from app.models.schemas import (
    SubscriptionResponse,
    CreateSubscriptionRequest,
    CancelSubscriptionRequest,
    ChangeSubscriptionRequest,
)

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


@router.get("/{workspace_id}", response_model=SubscriptionResponse)
async def get_subscription(workspace_id: str):
    """Get the current subscription for a workspace."""
    try:
        sub = await subscription_service.get_subscription(workspace_id)
        if not sub:
            raise HTTPException(status_code=404, detail="No active subscription found")
        return sub
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/", response_model=SubscriptionResponse)
async def create_subscription(request: CreateSubscriptionRequest):
    """Create a new subscription for a workspace."""
    try:
        return await subscription_service.create_subscription(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel", response_model=SubscriptionResponse)
async def cancel_subscription(request: CancelSubscriptionRequest):
    """Cancel a workspace's subscription."""
    try:
        return await subscription_service.cancel_subscription(request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{workspace_id}/change-plan", response_model=SubscriptionResponse)
async def change_plan(request: ChangeSubscriptionRequest):
    """Change the plan on an existing subscription."""
    try:
        return await subscription_service.change_plan(request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{workspace_id}/revoke-cancellation", response_model=SubscriptionResponse)
async def revoke_cancellation(workspace_id: str):
    """Revoke a pending end-of-period cancellation."""
    try:
        return await subscription_service.revoke_cancellation(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{workspace_id}/pause", response_model=SubscriptionResponse)
async def pause_subscription(workspace_id: str):
    """Pause a workspace's subscription."""
    try:
        return await subscription_service.pause_subscription(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{workspace_id}/resume", response_model=SubscriptionResponse)
async def resume_subscription(workspace_id: str):
    """Resume a paused subscription."""
    try:
        return await subscription_service.resume_subscription(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

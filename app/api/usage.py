"""Usage metering & reporting API routes."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import usage_service
from app.models.schemas import UsageEventRequest, UsageReportResponse

router = APIRouter(prefix="/usage", tags=["Usage"])


@router.post("/events")
async def record_usage_event(event: UsageEventRequest):
    """Record a usage event synchronously.

    Pushes to Stripe Meter and updates the Redis counter immediately.
    For high-throughput paths, prefer POST /usage/events/async.
    """
    try:
        result = await usage_service.record_usage_event(event)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/async")
async def publish_usage_event(event: UsageEventRequest):
    """Publish a usage event to SQS for async processing.

    Preferred for high-throughput events (AI messages, WhatsApp, email).
    Returns immediately after publishing to SQS.
    """
    try:
        message_id = await usage_service.publish_usage_event(event)
        return {"status": "queued", "sqs_message_id": message_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{workspace_id}", response_model=UsageReportResponse)
async def get_usage_report(
    workspace_id: str,
    start_time: Optional[datetime] = Query(None, description="Period start (defaults to current billing period)"),
    end_time: Optional[datetime] = Query(None, description="Period end (defaults to current billing period)"),
):
    """Get a usage report for a workspace.

    Returns aggregated usage across all meters for the given period.
    Defaults to the current billing period.
    """
    try:
        return await usage_service.get_usage_report(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

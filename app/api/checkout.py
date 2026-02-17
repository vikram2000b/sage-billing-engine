"""Checkout, invoices, and payment reconciliation API routes."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import checkout_service
from app.models.schemas import (
    CreateCheckoutRequest,
    CheckoutResponse,
    CreateRazorpayPaymentRequest,
    RazorpayPaymentResponse,
    InvoiceResponse,
    SendInvoiceRequest,
    ReconcilePaymentRequest,
)

router = APIRouter(prefix="/checkout", tags=["Checkout & Invoices"])


# ─── Checkout ───


@router.post("/session", response_model=CheckoutResponse)
async def create_checkout_session(request: CreateCheckoutRequest):
    """Create a checkout session (Stripe Checkout or Razorpay)."""
    try:
        return await checkout_service.create_checkout_session(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/razorpay", response_model=RazorpayPaymentResponse)
async def create_razorpay_payment(request: CreateRazorpayPaymentRequest):
    """Create a Razorpay order for an existing Stripe invoice.

    Used when a customer with a send_invoice subscription wants to pay via UPI/netbanking.
    """
    try:
        return await checkout_service.create_razorpay_payment(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Invoices ───


@router.get("/invoices/{workspace_id}", response_model=list[InvoiceResponse])
async def list_invoices(
    workspace_id: str,
    status: Optional[str] = Query(None, description="Filter by invoice status"),
):
    """List invoices for a workspace."""
    try:
        return await checkout_service.list_invoices(workspace_id, status=status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/invoices/detail/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(invoice_id: str):
    """Get invoice details."""
    try:
        return await checkout_service.get_invoice(invoice_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, request: SendInvoiceRequest):
    """Send an invoice to the customer via email or WhatsApp."""
    try:
        return await checkout_service.send_invoice(invoice_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Reconciliation ───


@router.post("/reconcile")
async def reconcile_payment(request: ReconcilePaymentRequest):
    """Manually reconcile a bank transfer / external payment against a Stripe invoice.

    Called by the finance team from the admin portal when they confirm
    a payment has been received via NEFT/RTGS/UPI/cheque.
    """
    try:
        return await checkout_service.reconcile_manual_payment(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

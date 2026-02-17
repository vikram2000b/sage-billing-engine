"""Checkout & invoice service — handles payment initiation across multiple gateways.

Supports:
  - Stripe Checkout (cards, international)
  - Razorpay (UPI, netbanking, Indian cards)
  - Manual bank transfer reconciliation
"""

from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logging import logger, tracer
from app.clients.stripe_client import stripe_client
from app.services.entitlement_service import invalidate_entitlements
from app.models.enums import PaymentGateway, InvoiceStatus
from app.models.schemas import (
    CreateCheckoutRequest,
    CheckoutResponse,
    CreateRazorpayPaymentRequest,
    RazorpayPaymentResponse,
    InvoiceResponse,
    SendInvoiceRequest,
    ReconcilePaymentRequest,
)


async def create_checkout_session(request: CreateCheckoutRequest) -> CheckoutResponse:
    """Create a checkout session via the appropriate gateway."""
    with tracer.start_as_current_span("checkout.create", attributes={
        "workspace_id": request.workspace_id,
        "gateway": request.gateway.value,
    }):
        if request.gateway == PaymentGateway.STRIPE:
            return await _create_stripe_checkout(request)
        elif request.gateway == PaymentGateway.RAZORPAY:
            # For Razorpay, we still create the subscription in Stripe (send_invoice mode)
            # and then create a Razorpay payment link for the first invoice
            return await _create_stripe_checkout(request)
        else:
            raise ValueError(f"Unsupported checkout gateway: {request.gateway}")


async def create_razorpay_payment(request: CreateRazorpayPaymentRequest) -> RazorpayPaymentResponse:
    """Create a Razorpay order for an existing Stripe invoice.

    Used when a customer with a send_invoice subscription wants to pay via Razorpay.
    """
    with tracer.start_as_current_span("checkout.razorpay_payment"):
        # Fetch the invoice amount from Stripe
        invoice = await stripe_client.get_invoice(request.stripe_invoice_id)

        # Lazy import razorpay only when needed
        import razorpay
        rz_client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        # Create Razorpay order
        order = rz_client.order.create({
            "amount": invoice.amount_due,  # Stripe stores in smallest currency unit
            "currency": invoice.currency.upper(),
            "notes": {
                "stripe_invoice_id": request.stripe_invoice_id,
                "workspace_id": request.workspace_id,
                "stripe_customer_id": invoice.customer,
            },
        })

        logger.info(
            f"Created Razorpay order {order['id']} for Stripe invoice {request.stripe_invoice_id}"
        )

        return RazorpayPaymentResponse(
            razorpay_order_id=order["id"],
            amount=invoice.amount_due,
            currency=invoice.currency.upper(),
            stripe_invoice_id=request.stripe_invoice_id,
        )


async def reconcile_manual_payment(request: ReconcilePaymentRequest) -> dict:
    """Reconcile a manual bank transfer against a Stripe invoice.

    Marks the Stripe invoice as paid out-of-band, which triggers:
      - invoice.paid webhook → entitlements activated
      - subscription status updated if it was past_due
    """
    with tracer.start_as_current_span("checkout.reconcile_manual"):
        # Mark as paid in Stripe
        invoice = await stripe_client.mark_invoice_paid_out_of_band(request.stripe_invoice_id)

        # Invalidate entitlement cache
        await invalidate_entitlements(request.workspace_id)

        logger.info(
            f"Reconciled manual payment for invoice {request.stripe_invoice_id}: "
            f"{request.transfer_method} ref={request.bank_reference}"
        )

        return {
            "status": "reconciled",
            "invoice_id": request.stripe_invoice_id,
            "invoice_status": invoice.status,
            "transfer_method": request.transfer_method,
            "bank_reference": request.bank_reference,
        }


async def get_invoice(invoice_id: str) -> InvoiceResponse:
    """Get invoice details."""
    with tracer.start_as_current_span("invoice.get"):
        inv = await stripe_client.get_invoice(invoice_id)
        return _stripe_invoice_to_response(inv)


async def list_invoices(workspace_id: str, status: Optional[str] = None) -> list[InvoiceResponse]:
    """List invoices for a workspace."""
    with tracer.start_as_current_span("invoice.list"):
        customer = await stripe_client.get_customer_by_workspace(workspace_id)
        if not customer:
            return []

        invoices = await stripe_client.list_invoices(
            customer_id=customer.id,
            status=status,
            limit=50,
        )
        return [_stripe_invoice_to_response(inv) for inv in invoices]


async def send_invoice(invoice_id: str, request: SendInvoiceRequest) -> dict:
    """Send an invoice to the customer."""
    with tracer.start_as_current_span("invoice.send"):
        if request.channel == "email":
            await stripe_client.send_invoice(invoice_id)
            return {"status": "sent", "channel": "email", "invoice_id": invoice_id}
        elif request.channel == "whatsapp":
            # Get the invoice PDF URL for WhatsApp delivery
            inv = await stripe_client.get_invoice(invoice_id)
            # TODO: Integrate with WhatsApp sending service
            logger.info(f"WhatsApp invoice delivery requested for {invoice_id}, PDF: {inv.invoice_pdf}")
            return {
                "status": "pending",
                "channel": "whatsapp",
                "invoice_id": invoice_id,
                "pdf_url": inv.invoice_pdf,
                "hosted_url": inv.hosted_invoice_url,
            }
        else:
            raise ValueError(f"Unsupported invoice delivery channel: {request.channel}")


# ─── Private helpers ───


async def _create_stripe_checkout(request: CreateCheckoutRequest) -> CheckoutResponse:
    """Create a Stripe Checkout session."""
    customer = await stripe_client.get_or_create_customer(
        workspace_id=request.workspace_id,
        email=request.email,
        metadata={"user_id": request.user_id},
    )

    session = await stripe_client.create_checkout_session(
        customer_id=customer.id,
        price_id=request.price_id,
        success_url=request.success_url,
        cancel_url=request.cancel_url,
        metadata={
            "workspace_id": request.workspace_id,
            "user_id": request.user_id,
            **request.metadata,
        },
    )

    return CheckoutResponse(
        checkout_url=session.url,
        session_id=session.id,
        gateway=PaymentGateway.STRIPE,
    )


def _stripe_invoice_to_response(inv) -> InvoiceResponse:
    """Convert a Stripe Invoice to our response schema."""
    workspace_id = (inv.get("metadata") or {}).get("workspace_id", "")

    try:
        status = InvoiceStatus(inv["status"])
    except (ValueError, KeyError):
        status = InvoiceStatus.DRAFT

    return InvoiceResponse(
        invoice_id=inv["id"],
        workspace_id=workspace_id,
        status=status,
        amount_due=inv.get("amount_due", 0),
        amount_paid=inv.get("amount_paid", 0),
        currency=inv.get("currency", "usd"),
        due_date=datetime.fromtimestamp(inv["due_date"], tz=timezone.utc) if inv.get("due_date") else None,
        pdf_url=inv.get("invoice_pdf"),
        hosted_url=inv.get("hosted_invoice_url"),
        created_at=datetime.fromtimestamp(inv["created"], tz=timezone.utc),
    )

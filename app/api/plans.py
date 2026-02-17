"""Plans catalog API — lists available products and prices from Stripe."""

from fastapi import APIRouter, HTTPException

from aiocache import cached

from app.clients.stripe_client import stripe_client

router = APIRouter(prefix="/plans", tags=["Plans"])


@router.get("/")
@cached(ttl=600)  # Cache for 10 minutes — plan catalog changes rarely
async def list_plans():
    """List all available subscription plans from Stripe.

    Returns products grouped by billing interval (monthly/yearly/one_time).
    """
    try:
        products = await stripe_client.list_products(active=True)
        prices = await stripe_client.list_prices(active=True)

        # Build a price lookup by product ID
        price_by_product: dict[str, list[dict]] = {}
        for price in prices:
            product_id = price["product"]
            if product_id not in price_by_product:
                price_by_product[product_id] = []
            price_by_product[product_id].append({
                "price_id": price["id"],
                "unit_amount": price.get("unit_amount"),
                "currency": price.get("currency"),
                "recurring": {
                    "interval": price["recurring"]["interval"],
                    "interval_count": price["recurring"]["interval_count"],
                } if price.get("recurring") else None,
                "type": price.get("type"),  # "recurring" or "one_time"
            })

        # Structure response
        plans = {
            "monthly": [],
            "yearly": [],
            "one_time": [],
        }

        for product in products:
            product_prices = price_by_product.get(product["id"], [])
            metadata = dict(product.get("metadata", {}))

            product_info = {
                "product_id": product["id"],
                "name": product["name"],
                "description": product.get("description"),
                "tier": metadata.get("tier", "starter"),
                "features": [f.strip() for f in metadata.get("features", "").split(",")] if metadata.get("features") else [],
                "prices": product_prices,
                "metadata": metadata,
            }

            for price in product_prices:
                if price.get("type") == "one_time":
                    plans["one_time"].append({**product_info, "price": price})
                elif price.get("recurring"):
                    interval = price["recurring"]["interval"]
                    if interval == "month":
                        plans["monthly"].append({**product_info, "price": price})
                    elif interval == "year":
                        plans["yearly"].append({**product_info, "price": price})

        return plans

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

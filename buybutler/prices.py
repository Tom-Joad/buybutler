"""Purchase prices: units, unit prices and the summary on a product.

Amounts are integer cents, never floats. A price may carry the quantity it
was for, as a number and a free unit; weights and volumes are converted, so
500 g for 2.49 and 1 kg for 4.50 can be compared per kg.
"""

from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal

# Units that convert into a common base, and the factor to it. Anything else
# is compared only with entries in the same unit (lower case, trimmed).
_BASE = {
    "g": ("kg", 0.001),
    "kg": ("kg", 1.0),
    "ml": ("l", 0.001),
    "cl": ("l", 0.01),
    "l": ("l", 1.0),
}
_PIECES = {"piece", "pieces", "pc", "pcs", "stk", "stück", "st"}


def base_unit(quantity: float | None, unit: str) -> tuple[float, str] | None:
    """The quantity in its comparison unit, e.g. (0.5, "kg") for 500 g."""
    if not quantity:
        return None
    key = unit.strip().casefold()
    if key in _BASE:
        base, factor = _BASE[key]
        return quantity * factor, base
    if key in _PIECES:
        return quantity, "piece"
    if not key:
        return None
    return quantity, key


def unit_price(row: sqlite3.Row) -> dict | None:
    """Price per base unit in cents (rounded), or None without a quantity."""
    based = base_unit(row["quantity"], row["unit"])
    if based is None:
        return None
    amount, per = based
    # Half up, as on a price tag; round() would round 872.5 to 872.
    cents = (Decimal(row["amount_cents"]) / Decimal(repr(amount))).quantize(Decimal(1), ROUND_HALF_UP)
    return {"amount_cents": int(cents), "per": per}


def to_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "rating_id": row["rating_id"],
        "user": row["username"],
        "amount_cents": row["amount_cents"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "unit_price": unit_price(row),
        "on_offer": bool(row["on_offer"]),
        "store": row["store"],
        "bought_on": row["bought_on"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


SELECT = "SELECT pr.*, u.username FROM prices pr LEFT JOIN users u ON u.id = pr.user_id"
ORDER = "ORDER BY pr.bought_on DESC, pr.created_at DESC, pr.id DESC"


def summary(rows: list[sqlite3.Row]) -> dict | None:
    """Last price, and the lowest and highest regular price comparable to it.

    `rows` are a product's prices, newest first. Offer prices never count as
    lowest or highest; prices count when they share the unit basis of the
    newest regular price (per kg, per litre, per piece, or no quantity).
    """
    if not rows:
        return None
    regular = [r for r in rows if not r["on_offer"]]
    lowest = highest = None
    basis = None
    if regular:
        reference = base_unit(regular[0]["quantity"], regular[0]["unit"])
        basis = reference[1] if reference else None

        def key(r: sqlite3.Row) -> float | None:
            based = base_unit(r["quantity"], r["unit"])
            if basis is None:
                return float(r["amount_cents"]) if based is None else None
            if based is None or based[1] != basis:
                return None
            return r["amount_cents"] / based[0]

        comparable = [(key(r), r) for r in regular]
        comparable = [(k, r) for k, r in comparable if k is not None]
        if comparable:
            lowest = min(comparable, key=lambda kr: kr[0])[1]
            highest = max(comparable, key=lambda kr: kr[0])[1]
    return {
        "last": to_json(rows[0]),
        "lowest": to_json(lowest) if lowest is not None else None,
        "highest": to_json(highest) if highest is not None else None,
        "basis": basis,
        "count": len(rows),
    }

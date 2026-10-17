"""The shopping list of a list: products and free entries, ticked off in the shop.

Items are named by the client_id the phone gives them, so a tick or a delete
made offline can name an item the server hasn't seen yet; the outbox sends
them in order. Ticking is last write wins, except that ticking an item that
is ticked already keeps who ticked it first.

The order in a store is remembered per item key: "p:<product id>" for a
product, "t:<text>" for a free entry. Reordering what is on the list now
keeps the remembered places of everything else (see set_order).
"""

from __future__ import annotations

import sqlite3
import uuid

from . import db

TEXT_MAX = 100
QUANTITY_MAX = 20
STORE_MAX = 100


class ShoppingError(ValueError):
    """A change that can't be made; the message says why."""


def item_key(product_id: int | None, text: str) -> str:
    if product_id is not None:
        return f"p:{product_id}"
    return "t:" + " ".join(text.split()).casefold()


SELECT = (
    "SELECT s.*, p.name AS product_name, p.brand, p.category, p.image, p.quantity AS product_quantity,"
    " cu.username AS checked_name, au.username AS added_name,"
    " r.buy_again, r.reason"
    " FROM shopping_items s"
    " LEFT JOIN products p ON p.id = s.product_id"
    " LEFT JOIN users cu ON cu.id = s.checked_by"
    " LEFT JOIN users au ON au.id = s.created_by"
    " LEFT JOIN ratings r ON r.id = (SELECT r2.id FROM ratings r2 WHERE r2.product_id = s.product_id"
    "  ORDER BY r2.rated_on DESC, r2.created_at DESC, r2.id DESC LIMIT 1)"
)


def _json(row: sqlite3.Row, image_url, position: int | None) -> dict:
    return {
        "id": row["client_id"],
        "product_id": row["product_id"],
        "name": row["product_name"] if row["product_id"] is not None else row["text"],
        "text": row["text"],
        "brand": row["brand"] or "",
        "category": row["category"] or "",
        "image": image_url("products", row["image"]),
        "verdict": None if row["buy_again"] is None else bool(row["buy_again"]),
        "reason": row["reason"] or "",
        "quantity": row["quantity"],
        "store": row["store"],
        "checked": row["checked_at"] is not None,
        "checked_at": row["checked_at"],
        "checked_by": row["checked_name"],
        "added_by": row["added_name"],
        "key": item_key(row["product_id"], row["text"]),
        "position": position,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def positions(conn: sqlite3.Connection, list_id: int, store: str) -> dict[str, int]:
    return {
        r["item_key"]: r["position"]
        for r in conn.execute(
            "SELECT item_key, position FROM store_order WHERE list_id = ? AND store = ?", (list_id, store)
        )
    }


def items(conn: sqlite3.Connection, list_id: int, store: str, image_url) -> list[dict]:
    """Every item of the list, with its place in the given store's order
    (None when it has none yet). The phone sorts and groups."""
    places = positions(conn, list_id, store)
    rows = conn.execute(SELECT + " WHERE s.list_id = ? ORDER BY s.created_at, s.id", (list_id,)).fetchall()
    return [_json(r, image_url, places.get(item_key(r["product_id"], r["text"]))) for r in rows]


def get(conn: sqlite3.Connection, list_id: int, client_id: str, image_url, store: str = "") -> dict | None:
    row = conn.execute(SELECT + " WHERE s.list_id = ? AND s.client_id = ?", (list_id, client_id)).fetchone()
    if row is None:
        return None
    return _json(row, image_url, positions(conn, list_id, store).get(item_key(row["product_id"], row["text"])))


def stores(conn: sqlite3.Connection, list_id: int) -> list[str]:
    """Stores known in this list: from ratings, prices and shopping items."""
    rows = conn.execute(
        "SELECT store FROM ratings r JOIN products p ON p.id = r.product_id WHERE p.list_id = ? AND store <> ''"
        " UNION SELECT store FROM prices pr JOIN products p ON p.id = pr.product_id WHERE p.list_id = ? AND store <> ''"
        " UNION SELECT store FROM shopping_items WHERE list_id = ? AND store <> ''"
        " UNION SELECT store FROM store_order WHERE list_id = ? AND store <> ''",
        (list_id, list_id, list_id, list_id),
    ).fetchall()
    seen: dict[str, str] = {}
    for (name,) in rows:
        seen.setdefault(name.casefold(), name)
    return sorted(seen.values(), key=str.casefold)


def add(conn: sqlite3.Connection, list_id: int, user_id: int, client_id: str | None, product_id: int | None,
        text: str, quantity: str, store: str) -> tuple[str, bool]:
    """Add an item. Returns (client_id, created). A product that is on the
    list already and not ticked is returned instead of added twice; a
    repeated request (same client_id) returns the first."""
    if client_id:
        row = conn.execute("SELECT list_id FROM shopping_items WHERE client_id = ?", (client_id,)).fetchone()
        if row is not None:
            if row["list_id"] != list_id:
                raise ShoppingError("this item ID is used already")
            return client_id, False
    if product_id is not None:
        if conn.execute("SELECT 1 FROM products WHERE id = ? AND list_id = ?", (product_id, list_id)).fetchone() is None:
            raise ShoppingError("product not found")
        open_row = conn.execute(
            "SELECT client_id FROM shopping_items WHERE list_id = ? AND product_id = ? AND checked_at IS NULL",
            (list_id, product_id),
        ).fetchone()
        if open_row is not None:
            return open_row["client_id"], False
        text = ""
    elif not text:
        raise ShoppingError("a product or a text is required")
    client_id = client_id or str(uuid.uuid4())
    stamp = db.now()
    conn.execute(
        "INSERT INTO shopping_items (list_id, product_id, text, quantity, store, created_by, client_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (list_id, product_id, text, quantity, store, user_id, client_id, stamp, stamp),
    )
    return client_id, True


def _row(conn: sqlite3.Connection, list_id: int, client_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM shopping_items WHERE list_id = ? AND client_id = ?", (list_id, client_id)
    ).fetchone()
    if row is None:
        raise LookupError("item not found")
    return row


def update(conn: sqlite3.Connection, list_id: int, client_id: str, user_id: int, changes: dict) -> None:
    row = _row(conn, list_id, client_id)
    stamp = db.now()
    sets: dict[str, object] = {}
    if "checked" in changes:
        if changes["checked"] and row["checked_at"] is None:
            sets |= {"checked_at": stamp, "checked_by": user_id}
        elif not changes["checked"] and row["checked_at"] is not None:
            if row["product_id"] is not None and conn.execute(
                "SELECT 1 FROM shopping_items WHERE list_id = ? AND product_id = ? AND checked_at IS NULL",
                (list_id, row["product_id"]),
            ).fetchone():
                # The product was put on the list again meanwhile: that
                # entry stands for it, this one goes.
                conn.execute("DELETE FROM shopping_items WHERE id = ?", (row["id"],))
                return
            sets |= {"checked_at": None, "checked_by": None}
    for field in ("quantity", "store"):
        if field in changes and changes[field] != row[field]:
            sets[field] = changes[field]
    if "text" in changes and row["product_id"] is None and changes["text"] and changes["text"] != row["text"]:
        sets["text"] = changes["text"]
    if not sets:
        return
    sets["updated_at"] = stamp
    conn.execute(
        f"UPDATE shopping_items SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), row["id"]),
    )


def remove(conn: sqlite3.Connection, list_id: int, client_id: str) -> bool:
    return conn.execute(
        "DELETE FROM shopping_items WHERE list_id = ? AND client_id = ?", (list_id, client_id)
    ).rowcount == 1


def clear_checked(conn: sqlite3.Connection, list_id: int) -> int:
    return conn.execute(
        "DELETE FROM shopping_items WHERE list_id = ? AND checked_at IS NOT NULL", (list_id,)
    ).rowcount


def set_order(conn: sqlite3.Connection, list_id: int, store: str, keys: list[str]) -> None:
    """Put the given keys (the items shown, top to bottom) in this order.

    Remembered keys that aren't shown keep their places: the shown ones take
    over the places they held, in the new order, and keys without a place
    yet follow at the end. So moving bread before milk today doesn't lose
    where the cheese goes next week.
    """
    keys = list(dict.fromkeys(keys))
    known = [
        r["item_key"] for r in conn.execute(
            "SELECT item_key FROM store_order WHERE list_id = ? AND store = ? ORDER BY position, item_key",
            (list_id, store),
        )
    ]
    shown = set(keys)
    order = list(known)
    slots = [i for i, key in enumerate(known) if key in shown]
    for slot, key in zip(slots, keys):
        order[slot] = key
    order += keys[len(slots):]
    stamp = db.now()
    for position, key in enumerate(order):
        conn.execute(
            "INSERT INTO store_order (list_id, store, item_key, position, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (list_id, store, item_key) DO UPDATE SET"
            " position = excluded.position,"
            " updated_at = CASE WHEN position = excluded.position THEN updated_at ELSE excluded.updated_at END",
            (list_id, store, key, position, stamp, stamp),
        )

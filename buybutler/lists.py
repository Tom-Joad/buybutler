"""Lists shared between users, and the rules that keep them consistent.

Every list keeps at least one owner; every installation keeps at least one
admin. Deleting a list removes its products with their ratings, prices and
pictures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, images


class ListError(ValueError):
    """A change that would break a rule; the message says which."""


def owners(conn: sqlite3.Connection, list_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM list_members WHERE list_id = ? AND role = 'owner'", (list_id,)
    ).fetchone()[0]


def pictures(conn: sqlite3.Connection, list_id: int) -> list[tuple[str, str]]:
    """(kind, file name) of every picture that belongs to the list."""
    found = [
        ("products", r[0])
        for r in conn.execute("SELECT image FROM products WHERE list_id = ? AND image IS NOT NULL", (list_id,))
    ]
    found += [
        ("ratings", r[0])
        for r in conn.execute(
            "SELECT r.photo FROM ratings r JOIN products p ON p.id = r.product_id"
            " WHERE p.list_id = ? AND r.photo IS NOT NULL",
            (list_id,),
        )
    ]
    return found


def delete_list(conn: sqlite3.Connection, list_id: int) -> list[tuple[str, str]]:
    """Delete a list and everything in it. Returns the pictures to remove
    once the transaction has committed."""
    files = pictures(conn, list_id)
    # Ratings and prices go with their products (ON DELETE CASCADE), the
    # memberships with the list.
    conn.execute("DELETE FROM products WHERE list_id = ?", (list_id,))
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    return files


def remove_files(root: Path, files: list[tuple[str, str]]) -> None:
    for kind, name in files:
        images.delete(root, kind, name)


def set_member_role(conn: sqlite3.Connection, list_id: int, user_id: int, role: str) -> None:
    current = conn.execute(
        "SELECT role FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id)
    ).fetchone()
    if current is None:
        raise ListError("not a member of this list")
    if current["role"] == "owner" and role != "owner" and owners(conn, list_id) == 1:
        raise ListError("a list needs at least one owner")
    conn.execute(
        "UPDATE list_members SET role = ?, updated_at = ? WHERE list_id = ? AND user_id = ?",
        (role, db.now(), list_id, user_id),
    )


def remove_member(conn: sqlite3.Connection, list_id: int, user_id: int) -> None:
    current = conn.execute(
        "SELECT role FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id)
    ).fetchone()
    if current is None:
        raise ListError("not a member of this list")
    if current["role"] == "owner" and owners(conn, list_id) == 1:
        raise ListError("a list needs at least one owner; make someone else owner first, or delete the list")
    conn.execute("DELETE FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int) -> list[tuple[str, str]]:
    """Delete a user. Lists only they belong to go with them; a list they
    alone own but share with others must get another owner first. Their
    ratings and prices stay, without a name."""
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ListError("no such user")
    if row["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1:
        raise ListError("the last admin can't be deleted")
    files: list[tuple[str, str]] = []
    for member in conn.execute(
        "SELECT list_id, role FROM list_members WHERE user_id = ?", (user_id,)
    ).fetchall():
        others = conn.execute(
            "SELECT COUNT(*) FROM list_members WHERE list_id = ? AND user_id != ?", (member["list_id"], user_id)
        ).fetchone()[0]
        if others == 0:
            files += delete_list(conn, member["list_id"])
        elif member["role"] == "owner" and owners(conn, member["list_id"]) == 1:
            name = conn.execute("SELECT name FROM lists WHERE id = ?", (member["list_id"],)).fetchone()[0]
            raise ListError(f"the user is the only owner of the shared list {name!r}; make someone else owner first")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return files

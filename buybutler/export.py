"""A readable copy of the lists, products, ratings and prices as files.

The database stays the only truth. This writes every list and product as a
Markdown file with a YAML header under `export/` (see FORMAT.md), so the
data can be read without Buybutler. The copy is for reading: it is never
read back, and a file changed by hand is overwritten.

Only what changed is written. The database counts changes per product and
list (`export_rev`, raised by triggers, see db.py); a run writes the files
of those whose count is ahead of `exported_rev`, flushes once, and then
records the counts it wrote. A run that dies before that is repeated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, files

log = logging.getLogger("buybutler")

PRODUCT_FORMAT = "buybutler/1"
LIST_FORMAT = "buybutler-list/1"
NOTE = "# Written by Buybutler: a copy for reading. Changes here are overwritten, see FORMAT.md."

_FOLDER = re.compile(r"(\d+)(?:-.*)?")
_TRASHED = re.compile(r"-(\d{8}-\d{6})(?:~\d+)?$")
_SPELLED = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
# What YAML doesn't take as it is inside double quotes, beyond what JSON
# escapes already.
_UNSAFE = re.compile("[\x7f-\x9f  \ud800-\udfff﻿￾￿]")
# Products read, rendered and written per read transaction.
_CHUNK = 500


def _quoted(value: str) -> str:
    """A string as a YAML double-quoted scalar. Always quoted, so a barcode
    keeps its leading zero and a date stays text."""
    return _UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(value, ensure_ascii=False))


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _line(key: str, value: str | None) -> str:
    return f"{key}: {_quoted(value)}\n" if value else ""


# Field names avoid `on`, `off`, `yes`, `no`, `y` and `n`: YAML 1.1 readers
# (PyYAML among them) take those for true and false, also as a key.
def _flow(pairs: list[tuple[str, str | None]]) -> str:
    return "  - {" + ", ".join(f"{k}: {v}" for k, v in pairs if v is not None) + "}\n"


def _text(value: str | None) -> str | None:
    return _quoted(value) if value else None


def slug(name: str) -> str:
    """A name as part of a folder name: lower case, a-z, 0-9 and dashes."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().translate(_SPELLED)).strip("-")[:60].rstrip("-")


def folder_name(record_id: int, name: str) -> str:
    tail = slug(name)
    return f"{record_id}-{tail}" if tail else str(record_id)


def render_list(row: sqlite3.Row, members: list[sqlite3.Row], currency: str) -> str:
    out = ["---\n", NOTE + "\n", f"format: {_quoted(LIST_FORMAT)}\n", f"id: {row['id']}\n",
           _line("name", row["name"]), _line("currency", currency),
           _line("created", row["created_at"]), _line("updated", row["updated_at"]),
           _line("created_by", row["username"])]
    if members:
        out.append("members:\n")
        out += [_flow([("user", _quoted(m["username"])), ("role", _quoted(m["role"])),
                       ("since", _quoted(m["created_at"]))]) for m in members]
    out.append("---\n")
    return "".join(out)


def render_product(row: sqlite3.Row, ratings: list[sqlite3.Row], prices: list[sqlite3.Row]) -> str:
    # From export/<list>/products/<product>/ up to the data folder.
    pictures = "../../../../images/"
    out = ["---\n", NOTE + "\n", f"format: {_quoted(PRODUCT_FORMAT)}\n", f"id: {row['id']}\n",
           _line("list", row["list_name"]), _line("name", row["name"]), _line("brand", row["brand"]),
           _line("barcode", row["barcode"]), _line("category", row["category"]),
           _line("quantity", row["quantity"]), _line("source", row["source"]),
           _line("photo", pictures + "products/" + row["image"] if row["image"] else None),
           _line("created", row["created_at"]), _line("updated", row["updated_at"]),
           _line("created_by", row["username"])]
    if ratings:
        out.append("ratings:\n")
        out += [_flow([
            ("id", str(r["id"])), ("date", _quoted(r["rated_on"])), ("by", _text(r["username"])),
            ("buy_again", "true" if r["buy_again"] else "false"),
            ("stars", str(r["stars"]) if r["stars"] is not None else None),
            ("store", _text(r["store"])), ("reason", _text(r["reason"])),
            ("photo", _text(pictures + "ratings/" + r["photo"] if r["photo"] else None)),
            ("created", _quoted(r["created_at"])), ("updated", _quoted(r["updated_at"])),
        ]) for r in ratings]
    if prices:
        out.append("prices:\n")
        out += [_flow([
            ("id", str(p["id"])), ("date", _quoted(p["bought_on"])), ("by", _text(p["username"])),
            ("amount", _amount(p["amount_cents"])),
            ("quantity", _number(p["quantity"]) if p["quantity"] is not None else None),
            ("unit", _text(p["unit"])), ("offer", "true" if p["on_offer"] else None),
            ("store", _text(p["store"])),
            ("rating", str(p["rating_id"]) if p["rating_id"] is not None else None),
            ("created", _quoted(p["created_at"])), ("updated", _quoted(p["updated_at"])),
        ]) for p in prices]
    out.append("---\n")
    return "".join(out)


def _folders(parent: Path) -> dict[int, Path]:
    """The record folders in `parent` by the ID their name starts with."""
    found: dict[int, Path] = {}
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                m = _FOLDER.fullmatch(entry.name)
                if m and entry.is_dir(follow_symlinks=False):
                    found[int(m.group(1))] = Path(entry.path)
    except FileNotFoundError:
        pass
    return found


class Export:
    def __init__(self, settings: config.Settings):
        self.settings = settings
        self.root = settings.export_dir
        self.tmp = self.root / ".tmp"
        # One run at a time in this process; a run of the command line next
        # to it is harmless, both write the same text.
        self._lock = threading.Lock()

    # ---- one run -----------------------------------------------------------

    def run(self, everything: bool = False, check: bool = False) -> dict[str, int]:
        """Write what changed since the last run. `check` also writes files
        that are missing (at start), `everything` compares every file with
        the database (the `export` command). Returns what was done."""
        with self._lock:
            conn = db.connect(self.settings.database)
            try:
                return self._run(conn, everything, check)
            finally:
                conn.close()

    def _run(self, conn: sqlite3.Connection, everything: bool, check: bool) -> dict[str, int]:
        done = {"lists": 0, "products": 0, "removed": 0}
        pending = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM products WHERE export_rev > exported_rev)"
            " OR EXISTS (SELECT 1 FROM lists WHERE export_rev > exported_rev)"
            " OR EXISTS (SELECT 1 FROM export_gone)"
        ).fetchone()[0]
        if not (pending or everything or check):
            return done
        self.tmp.mkdir(parents=True, exist_ok=True)
        list_dirs = _folders(self.root)
        product_dirs = {list_id: _folders(path / "products") for list_id, path in list_dirs.items()}

        # What was deleted goes first: SQLite may give the ID of the newest
        # row, once deleted, to the next one.
        # Lists before products, so a deleted list goes as one folder with
        # its products in it.
        gone = conn.execute("SELECT id, kind, record_id FROM export_gone ORDER BY id").fetchall()
        for row in sorted(gone, key=lambda r: r["kind"] != "list"):
            if row["kind"] == "list":
                path = list_dirs.pop(row["record_id"], None)
                product_dirs.pop(row["record_id"], None)
            else:
                path = next((d.pop(row["record_id"]) for d in product_dirs.values() if row["record_id"] in d), None)
            if path is not None:
                self._to_trash(path)
                done["removed"] += 1

        # Lists are few: all of them are compared, so a renamed list moves
        # its folder and a changed BUYBUTLER_CURRENCY reaches list.md.
        lists = conn.execute(
            "SELECT l.*, u.username FROM lists l LEFT JOIN users u ON u.id = l.created_by ORDER BY l.id"
        ).fetchall()
        list_revs = []
        for row in lists:
            path = self._place(list_dirs.get(row["id"]), self.root / folder_name(row["id"], row["name"]))
            if list_dirs.get(row["id"]) not in (None, path):
                product_dirs[row["id"]] = _folders(path / "products")
            list_dirs[row["id"]] = path
            members = conn.execute(
                "SELECT u.username, m.role, m.created_at FROM list_members m JOIN users u ON u.id = m.user_id"
                " WHERE m.list_id = ? ORDER BY m.created_at, u.username", (row["id"],)
            ).fetchall()
            if files.write_if_changed(path / "list.md", render_list(row, members, self.settings.currency), self.tmp):
                done["lists"] += 1
            list_revs.append((row["export_rev"], row["id"], row["export_rev"]))

        if everything:
            ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id")]
        else:
            ids = [r[0] for r in conn.execute("SELECT id FROM products WHERE export_rev > exported_rev ORDER BY id")]
            if check:
                have = {i for d in product_dirs.values() for i in d}
                ids = sorted({*ids, *(r[0] for r in conn.execute("SELECT id FROM products") if r[0] not in have)})
        product_revs = []
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            # One snapshot per chunk, so a product and its ratings and
            # prices are read as of the same moment.
            conn.execute("BEGIN")
            try:
                texts = self._render(conn, chunk)
            finally:
                conn.execute("COMMIT")
            for row, text in texts:
                # A product of a list this run hasn't seen was created
                # after the lists were read; it stays pending for the next.
                if row["list_id"] not in list_dirs:
                    continue
                old = next((d.pop(row["id"]) for d in product_dirs.values() if row["id"] in d), None)
                path = self._place(old, list_dirs[row["list_id"]] / "products" / folder_name(row["id"], row["name"]))
                product_dirs.setdefault(row["list_id"], {})[row["id"]] = path
                if files.write_if_changed(path / "product.md", text, self.tmp):
                    done["products"] += 1
                product_revs.append((row["export_rev"], row["id"], row["export_rev"]))

        if any(done.values()):
            # Only now are the files safe, and only then may the database
            # say so.
            files.sync_tree(self.root)
        with conn:
            conn.executemany("UPDATE lists SET exported_rev = ? WHERE id = ? AND exported_rev < ?", list_revs)
            conn.executemany("UPDATE products SET exported_rev = ? WHERE id = ? AND exported_rev < ?", product_revs)
            if gone:
                conn.execute("DELETE FROM export_gone WHERE id <= ?", (gone[-1]["id"],))
        return done

    def _render(self, conn: sqlite3.Connection, ids: list[int]) -> list[tuple[sqlite3.Row, str]]:
        marks = ",".join("?" * len(ids))
        products = conn.execute(
            "SELECT p.*, l.name AS list_name, u.username FROM products p JOIN lists l ON l.id = p.list_id"
            f" LEFT JOIN users u ON u.id = p.created_by WHERE p.id IN ({marks}) ORDER BY p.id", ids
        ).fetchall()
        ratings: dict[int, list[sqlite3.Row]] = {}
        for r in conn.execute(
            "SELECT r.*, u.username FROM ratings r LEFT JOIN users u ON u.id = r.user_id"
            f" WHERE r.product_id IN ({marks}) ORDER BY r.rated_on, r.created_at, r.id", ids
        ):
            ratings.setdefault(r["product_id"], []).append(r)
        prices: dict[int, list[sqlite3.Row]] = {}
        for p in conn.execute(
            "SELECT pr.*, u.username FROM prices pr LEFT JOIN users u ON u.id = pr.user_id"
            f" WHERE pr.product_id IN ({marks}) ORDER BY pr.bought_on, pr.created_at, pr.id", ids
        ):
            prices.setdefault(p["product_id"], []).append(p)
        return [(row, render_product(row, ratings.get(row["id"], []), prices.get(row["id"], []))) for row in products]

    def _place(self, old: Path | None, wanted: Path) -> Path:
        """The folder of a record under its current name: created, or moved
        there when the record was renamed."""
        if old is not None and old != wanted and not wanted.exists():
            wanted.parent.mkdir(parents=True, exist_ok=True)
            old.rename(wanted)
        else:
            # Also when both exist (a folder copied by hand carries the same
            # ID): the right name wins, the other is left alone.
            wanted.mkdir(parents=True, exist_ok=True)
        return wanted

    def _to_trash(self, path: Path) -> None:
        trash = self.settings.trash
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f"{path.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        n = 2
        while target.exists():
            target, n = target.with_name(f"{target.name.rsplit('~', 1)[0]}~{n}"), n + 1
        shutil.move(str(path), target)

    # ---- housekeeping at start ---------------------------------------------

    def prepare(self) -> None:
        """The description of the format next to the files, and no leftovers
        of a run that died."""
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(exist_ok=True)
        doc = Path(__file__).with_name("FORMAT.md")
        if doc.exists():
            files.write_if_changed(self.root / "FORMAT.md", doc.read_text(encoding="utf-8"), self.tmp)

    def empty_trash(self) -> int:
        """Remove deleted records older than BUYBUTLER_TRASH_DAYS (0 keeps
        them); returns how many."""
        days, trash = self.settings.trash_days, self.settings.trash
        if days <= 0 or not trash.is_dir():
            return 0
        limit = datetime.now() - timedelta(days=days)
        removed = 0
        for folder in trash.iterdir():
            m = _TRASHED.search(folder.name)
            if not folder.is_dir() or not m:
                continue
            try:
                when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if when < limit:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed


class Exporter:
    """Runs the export in the background of the web app: once at start, then
    BUYBUTLER_EXPORT_SECONDS after the first change, so several changes to a
    product in that time are written as one file."""

    def __init__(self, settings: config.Settings):
        self.export = Export(settings)
        self.delay = settings.export_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="export", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(10)

    def notify(self) -> None:
        """Something was written to the database; maybe it is content."""
        self._wake.set()

    def _loop(self) -> None:
        self._once(start=True)
        while True:
            self._wake.wait()
            if self._stop.wait(self.delay):
                return
            self._wake.clear()
            self._once()

    def _once(self, start: bool = False) -> None:
        began = time.monotonic()
        try:
            if start:
                self.export.prepare()
                trashed = self.export.empty_trash()
                if trashed:
                    log.info("trash_emptied", extra={"removed": trashed})
            done = self.export.run(check=start)
        except Exception:  # noqa: BLE001  (the next run tries again)
            log.exception("export_failed")
            return
        if any(done.values()):
            log.info("export_written", extra={**done, "seconds": round(time.monotonic() - began, 2)})

"""Product data from Open Food Facts.

Only the barcode leaves the server; answers are cached in the database, so
scanning the same product again needs no request. The product picture is
downloaded once and served by Buybutler itself, so the phone never talks to
Open Food Facts directly.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from . import __version__, db

log = logging.getLogger(__name__)

API = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
USER_AGENT = f"Buybutler/{__version__} (self-hosted; https://github.com/Tom-Joad/buybutler)"

# EAN-8, UPC-A, EAN-13 and GTIN-14 are all digits.
BARCODE = re.compile(r"^\d{8,14}$")

FOUND_TTL = timedelta(days=30)
MISSING_TTL = timedelta(days=1)
IMAGE_LIMIT = 5 * 1024 * 1024


@dataclass(frozen=True)
class Suggestion:
    barcode: str
    name: str
    brand: str
    category: str
    quantity: str
    image_url: str


def valid_barcode(barcode: str) -> bool:
    return bool(BARCODE.match(barcode))


def _pick_name(product: dict, language: str) -> str:
    for key in (f"product_name_{language}", "product_name", "generic_name"):
        value = str(product.get(key) or "").strip()
        if value:
            return value
    return ""


def _parse(barcode: str, product: dict, language: str) -> Suggestion:
    brand = str(product.get("brands") or "").split(",")[0].strip()
    # Categories run from general to specific; the last one says most.
    categories = [c.strip() for c in str(product.get("categories") or "").split(",") if c.strip()]
    category = categories[-1] if categories else ""
    if ":" in category:  # an untranslated tag such as "de:joghurt"
        category = category.split(":", 1)[1].replace("-", " ")
    image = str(product.get("image_front_url") or product.get("image_url") or "")
    if not _image_url_allowed(image):
        image = ""
    return Suggestion(
        barcode=barcode,
        name=_pick_name(product, language)[:200],
        brand=brand[:200],
        category=category[:100],
        quantity=str(product.get("quantity") or "").strip()[:50],
        image_url=image,
    )


def _image_url_allowed(url: str) -> bool:
    """Pictures are only fetched from Open Food Facts' own image host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "openfoodfacts.org" or host.endswith(".openfoodfacts.org"))


def _cached(conn: sqlite3.Connection, barcode: str) -> tuple[bool, dict | None] | None:
    row = conn.execute("SELECT found, data, fetched_at FROM off_cache WHERE barcode = ?", (barcode,)).fetchone()
    if row is None:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
    ttl = FOUND_TTL if row["found"] else MISSING_TTL
    if datetime.now(timezone.utc) - fetched > ttl:
        return None
    return bool(row["found"]), json.loads(row["data"]) if row["data"] else None


def _store(conn: sqlite3.Connection, barcode: str, found: bool, data: dict | None) -> None:
    conn.execute(
        "INSERT INTO off_cache (barcode, found, data, fetched_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (barcode) DO UPDATE SET found = excluded.found, data = excluded.data,"
        " fetched_at = excluded.fetched_at",
        (barcode, int(found), json.dumps(data) if data is not None else None, db.now()),
    )


class Client:
    def __init__(self, language: str, transport: httpx.BaseTransport | None = None) -> None:
        self.language = language
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(8.0),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def lookup(self, conn: sqlite3.Connection, barcode: str) -> Suggestion | None:
        """Product data for a barcode, or None when Open Food Facts doesn't know it.

        Raises httpx.HTTPError when Open Food Facts can't be reached.
        """
        cached = _cached(conn, barcode)
        if cached is not None:
            found, data = cached
            log.debug("lookup_cached", extra={"found": found})
            return Suggestion(**data) if found and data else None

        fields = ",".join(
            [
                "product_name",
                f"product_name_{self.language}",
                "generic_name",
                "brands",
                "categories",
                "quantity",
                "image_front_url",
                "image_url",
            ]
        )
        response = self._http.get(
            API.format(barcode=barcode), params={"fields": fields, "lc": self.language}
        )
        if response.status_code == 404:
            _store(conn, barcode, False, None)
            log.info("lookup_done", extra={"found": False})
            return None
        response.raise_for_status()
        body = response.json()
        product = body.get("product") if isinstance(body, dict) else None
        if body.get("status") != 1 or not isinstance(product, dict):
            _store(conn, barcode, False, None)
            log.info("lookup_done", extra={"found": False})
            return None
        suggestion = _parse(barcode, product, self.language)
        _store(conn, barcode, True, asdict(suggestion))
        log.info("lookup_done", extra={"found": True})
        return suggestion

    def cached_suggestion(self, conn: sqlite3.Connection, barcode: str) -> Suggestion | None:
        row = conn.execute("SELECT found, data FROM off_cache WHERE barcode = ?", (barcode,)).fetchone()
        if row is None or not row["found"] or not row["data"]:
            return None
        return Suggestion(**json.loads(row["data"]))

    def fetch_image(self, url: str) -> bytes | None:
        """Download a product picture; None when that fails, which is not an error."""
        if not _image_url_allowed(url):
            return None
        try:
            with self._http.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                data = bytearray()
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > IMAGE_LIMIT:
                        return None
                return bytes(data)
        except httpx.HTTPError:
            log.warning("image_download_failed", exc_info=True)
            return None

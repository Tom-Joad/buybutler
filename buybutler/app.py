"""Web app: login, products, ratings, pictures and the interface."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__, auth, config, db, images, off

log = logging.getLogger("buybutler")

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "buybutler_session"
# Every request that changes something must carry this header. A form or
# link on another site can't set it, which is what stops cross-site
# requests; the SameSite cookie is the second line.
CSRF_HEADER = "x-buybutler"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
        "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


# ---------------------------------------------------------------------------
# Request and response shapes


def _text(value: str | None, limit: int) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise ValueError(f"at most {limit} characters")
    return value


class Login(BaseModel):
    username: str = Field(max_length=auth.USERNAME_MAX)
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


class ProductIn(BaseModel):
    barcode: str | None = None
    name: str
    brand: str = ""
    category: str = ""
    quantity: str = ""
    # Take the picture Open Food Facts has for this barcode (looked up
    # before); the client never passes a URL the server would fetch.
    use_suggested_image: bool = False

    @field_validator("barcode")
    @classmethod
    def _barcode(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not off.valid_barcode(v):
            raise ValueError("a barcode has 8 to 14 digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = _text(v, 200)
        if not v:
            raise ValueError("the name is required")
        return v

    @field_validator("brand")
    @classmethod
    def _brand(cls, v: str) -> str:
        return _text(v, 200)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return _text(v, 100)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v: str) -> str:
        return _text(v, 50)


class ProductPatch(BaseModel):
    barcode: str | None = None
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    quantity: str | None = None

    @field_validator("barcode")
    @classmethod
    def _barcode(cls, v: str | None) -> str | None:
        return ProductIn._barcode(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else ProductIn._name(v)

    @field_validator("brand")
    @classmethod
    def _brand(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 200)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 100)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 50)


class RatingIn(BaseModel):
    buy_again: bool
    stars: int | None = Field(default=None, ge=1, le=5)
    reason: str = ""
    store: str = ""
    rated_on: date | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        return _text(v, 2000)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str) -> str:
        return _text(v, 100)


class RatingPatch(BaseModel):
    buy_again: bool | None = None
    stars: int | None = Field(default=None, ge=1, le=5)
    clear_stars: bool = False
    reason: str | None = None
    store: str | None = None
    rated_on: date | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 2000)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 100)


# ---------------------------------------------------------------------------
# Rows as JSON


def _image_url(kind: str, name: str | None) -> str | None:
    return f"/images/{kind}/{name}" if name else None


def _rating(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "user": row["username"],
        "buy_again": bool(row["buy_again"]),
        "stars": row["stars"],
        "reason": row["reason"],
        "store": row["store"],
        "rated_on": row["rated_on"],
        "photo": _image_url("ratings", row["photo"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


RATING_SELECT = (
    "SELECT r.*, u.username FROM ratings r LEFT JOIN users u ON u.id = r.user_id"
)

# The verdict that counts is the newest rating: by the day it was given,
# then by when it was entered.
LATEST = (
    "(SELECT r.id FROM ratings r WHERE r.product_id = p.id"
    " ORDER BY r.rated_on DESC, r.created_at DESC, r.id DESC LIMIT 1)"
)


def _product(row: sqlite3.Row, latest: sqlite3.Row | None, count: int) -> dict:
    return {
        "id": row["id"],
        "barcode": row["barcode"],
        "name": row["name"],
        "brand": row["brand"],
        "category": row["category"],
        "quantity": row["quantity"],
        "image": _image_url("products", row["image"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "rating": _rating(latest) if latest is not None else None,
        "rating_count": count,
    }


def _load_product(conn: sqlite3.Connection, user: auth.User, product_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND household_id = ?", (product_id, user.household_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "product not found")
    return row


def _product_json(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    latest = conn.execute(
        RATING_SELECT + " WHERE r.product_id = ? ORDER BY r.rated_on DESC, r.created_at DESC, r.id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM ratings WHERE product_id = ?", (row["id"],)).fetchone()[0]
    return _product(row, latest, count)


def _load_rating(conn: sqlite3.Connection, user: auth.User, rating_id: int) -> sqlite3.Row:
    row = conn.execute(
        RATING_SELECT + " JOIN products p ON p.id = r.product_id WHERE r.id = ? AND p.household_id = ?",
        (rating_id, user.household_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "rating not found")
    return row


# ---------------------------------------------------------------------------
# App


def create_app(settings: config.Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or config.load()
    throttle = auth.LoginThrottle()
    lookup_client = off.Client(settings.off_language, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        before, after = db.migrate(settings.database)
        if before != after:
            log.info("database_migrated", extra={"from_version": before, "to_version": after})
        with db.session(settings.database) as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if users == 0:
            log.warning(
                "no_user_yet",
                extra={"hint": "create one: docker exec -it buybutler buybutler user add <name>"},
            )
        yield
        lookup_client.close()
        log.info("shutting down")

    app = FastAPI(title="Buybutler", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            response: Response = JSONResponse({"detail": "missing request header"}, status_code=403)
        else:
            response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/images/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def conn_dep() -> Iterator[sqlite3.Connection]:
        with db.session(settings.database) as conn:
            yield conn

    Conn = Annotated[sqlite3.Connection, Depends(conn_dep)]

    def user_dep(request: Request, conn: Conn) -> auth.User:
        token = request.cookies.get(COOKIE)
        user = auth.session_user(conn, token, settings.session_days) if token else None
        if user is None:
            raise HTTPException(401, "not logged in")
        return user

    CurrentUser = Annotated[auth.User, Depends(user_dep)]

    def secure(request: Request) -> bool:
        # Behind the reverse proxy, uvicorn takes the scheme from
        # X-Forwarded-Proto; a plain-HTTP test on the LAN still gets a cookie.
        return request.url.scheme == "https"

    # ---- health and session ------------------------------------------------

    @app.get("/health")
    def health(conn: Conn) -> dict:
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": __version__}

    @app.post("/api/session")
    def login(body: Login, request: Request, response: Response, conn: Conn) -> dict:
        client = request.client.host if request.client else "unknown"
        wait = throttle.retry_after(client)
        if wait:
            log.warning("login_throttled", extra={"client": client})
            raise HTTPException(429, "too many failed logins, try again later", headers={"Retry-After": str(wait)})
        user = auth.authenticate(conn, body.username, body.password)
        if user is None:
            throttle.failed(client)
            log.warning("login_failed", extra={"client": client})
            raise HTTPException(401, "wrong user name or password")
        throttle.succeeded(client)
        token = auth.start_session(conn, user.id, settings.session_days)
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=secure(request),
            path="/",
        )
        log.info("login_succeeded", extra={"user_id": user.id, "client": client})
        return {"username": user.username, "role": user.role}

    @app.delete("/api/session", status_code=204)
    def logout(request: Request, response: Response, conn: Conn) -> None:
        token = request.cookies.get(COOKIE)
        if token:
            auth.end_session(conn, token)
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/session")
    def me(user: CurrentUser) -> dict:
        return {"username": user.username, "role": user.role}

    @app.post("/api/session/password", status_code=204)
    def change_password(body: PasswordChange, user: CurrentUser, request: Request, response: Response,
                        conn: Conn) -> None:
        if auth.authenticate(conn, user.username, body.current) is None:
            raise HTTPException(400, "the current password is wrong")
        try:
            auth.set_password(conn, user.id, body.new)
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from None
        # Every session ended with the change; this one continues with a new token.
        token = auth.start_session(conn, user.id, settings.session_days)
        response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
                            samesite="lax", secure=secure(request), path="/")
        log.info("password_changed", extra={"user_id": user.id})

    # ---- lookup ------------------------------------------------------------

    @app.get("/api/lookup")
    def lookup(user: CurrentUser, conn: Conn, barcode: str = Query(max_length=14)) -> dict:
        barcode = barcode.strip()
        if not off.valid_barcode(barcode):
            raise HTTPException(422, "a barcode has 8 to 14 digits")
        row = conn.execute(
            "SELECT * FROM products WHERE household_id = ? AND barcode = ?", (user.household_id, barcode)
        ).fetchone()
        if row is not None:
            return {"product": _product_json(conn, row), "suggestion": None, "lookup": "known"}
        if not settings.off_lookup:
            return {"product": None, "suggestion": None, "lookup": "disabled"}
        try:
            suggestion = lookup_client.lookup(conn, barcode)
        except (httpx.HTTPError, ValueError):
            log.warning("lookup_failed", exc_info=True)
            return {"product": None, "suggestion": None, "lookup": "failed"}
        if suggestion is None:
            return {"product": None, "suggestion": None, "lookup": "not_found"}
        data = {k: v for k, v in suggestion.__dict__.items() if k != "image_url"}
        data["has_image"] = bool(suggestion.image_url)
        return {"product": None, "suggestion": data, "lookup": "found"}

    # ---- products ----------------------------------------------------------

    @app.get("/api/products")
    def list_products(
        user: CurrentUser,
        conn: Conn,
        verdict: Literal["all", "buy", "skip", "unrated"] = "all",
        q: str = Query("", max_length=200),
        store: str = Query("", max_length=100),
        category: str = Query("", max_length=100),
        sort: Literal["recent", "name", "stars"] = "recent",
    ) -> dict:
        where = ["p.household_id = ?"]
        args: list = [user.household_id]
        if verdict == "buy":
            where.append("lr.buy_again = 1")
        elif verdict == "skip":
            where.append("lr.buy_again = 0")
        elif verdict == "unrated":
            where.append("lr.id IS NULL")
        if q.strip():
            like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(p.name LIKE ? ESCAPE '\\' OR p.brand LIKE ? ESCAPE '\\' OR p.category LIKE ? ESCAPE '\\'"
                " OR p.barcode LIKE ? ESCAPE '\\' OR lr.reason LIKE ? ESCAPE '\\')"
            )
            args += [like] * 5
        if store.strip():
            where.append("EXISTS (SELECT 1 FROM ratings s WHERE s.product_id = p.id AND s.store = ? COLLATE NOCASE)")
            args.append(store.strip())
        if category.strip():
            where.append("p.category = ? COLLATE NOCASE")
            args.append(category.strip())
        order = {
            "recent": "COALESCE(lr.rated_on, substr(p.created_at, 1, 10)) DESC, COALESCE(lr.created_at, p.created_at) DESC",
            "name": "p.name COLLATE NOCASE ASC",
            "stars": "lr.stars IS NULL, lr.stars DESC, p.name COLLATE NOCASE ASC",
        }[sort]
        rows = conn.execute(
            "SELECT p.*, lr.id AS lr_id, (SELECT COUNT(*) FROM ratings c WHERE c.product_id = p.id) AS rating_count"
            f" FROM products p LEFT JOIN ratings lr ON lr.id = {LATEST}"
            f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 1000",
            args,
        ).fetchall()
        ids = [r["lr_id"] for r in rows if r["lr_id"] is not None]
        latest: dict[int, sqlite3.Row] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(RATING_SELECT + f" WHERE r.id IN ({marks})", ids):
                latest[r["id"]] = r
        return {"products": [_product(r, latest.get(r["lr_id"]), r["rating_count"]) for r in rows]}

    @app.get("/api/facets")
    def facets(user: CurrentUser, conn: Conn) -> dict:
        """Stores and categories used so far, for filters and suggestions."""
        stores = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT r.store FROM ratings r JOIN products p ON p.id = r.product_id"
                " WHERE p.household_id = ? AND r.store != '' ORDER BY r.store COLLATE NOCASE",
                (user.household_id,),
            )
        ]
        categories = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT category FROM products WHERE household_id = ? AND category != ''"
                " ORDER BY category COLLATE NOCASE",
                (user.household_id,),
            )
        ]
        return {"stores": stores, "categories": categories}

    @app.post("/api/products", status_code=201)
    def create_product(body: ProductIn, user: CurrentUser, conn: Conn) -> dict:
        if body.barcode and conn.execute(
            "SELECT 1 FROM products WHERE household_id = ? AND barcode = ?", (user.household_id, body.barcode)
        ).fetchone():
            raise HTTPException(409, "a product with this barcode exists already")
        image = None
        source = "manual"
        if body.use_suggested_image and body.barcode:
            suggestion = lookup_client.cached_suggestion(conn, body.barcode)
            if suggestion is not None:
                source = "openfoodfacts"
                data = lookup_client.fetch_image(suggestion.image_url) if suggestion.image_url else None
                if data:
                    try:
                        image = images.save(settings.images, "products", data)
                    except images.ImageError:
                        image = None
        stamp = db.now()
        product_id = conn.execute(
            "INSERT INTO products (household_id, barcode, name, brand, category, quantity, image, source,"
            " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user.household_id, body.barcode, body.name, body.brand, body.category, body.quantity, image,
             source, user.id, stamp, stamp),
        ).lastrowid
        log.info("product_created", extra={"product_id": product_id, "source": source})
        return _product_json(conn, _load_product(conn, user, product_id))

    @app.get("/api/products/{product_id}")
    def get_product(product_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_product(conn, user, product_id)
        data = _product_json(conn, row)
        data["ratings"] = [
            _rating(r)
            for r in conn.execute(
                RATING_SELECT + " WHERE r.product_id = ? ORDER BY r.rated_on DESC, r.created_at DESC, r.id DESC",
                (product_id,),
            )
        ]
        return data

    @app.patch("/api/products/{product_id}")
    def update_product(product_id: int, body: ProductPatch, user: CurrentUser, conn: Conn) -> dict:
        _load_product(conn, user, product_id)
        changes = body.model_dump(exclude_unset=True)
        if "barcode" in changes and changes["barcode"] and conn.execute(
            "SELECT 1 FROM products WHERE household_id = ? AND barcode = ? AND id != ?",
            (user.household_id, changes["barcode"], product_id),
        ).fetchone():
            raise HTTPException(409, "a product with this barcode exists already")
        changes = {k: v for k, v in changes.items() if v is not None or k == "barcode"}
        if changes:
            columns = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(
                f"UPDATE products SET {columns}, updated_at = ? WHERE id = ?",
                [*changes.values(), db.now(), product_id],
            )
            log.info("product_updated", extra={"product_id": product_id})
        return _product_json(conn, _load_product(conn, user, product_id))

    @app.delete("/api/products/{product_id}", status_code=204)
    def delete_product(product_id: int, user: CurrentUser, conn: Conn) -> None:
        row = _load_product(conn, user, product_id)
        photos = [r[0] for r in conn.execute("SELECT photo FROM ratings WHERE product_id = ?", (product_id,))]
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        images.delete(settings.images, "products", row["image"])
        for name in photos:
            images.delete(settings.images, "ratings", name)
        log.info("product_deleted", extra={"product_id": product_id, "ratings": len(photos)})

    async def read_picture(request: Request) -> bytes:
        limit = settings.max_upload_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, f"pictures may have at most {settings.max_upload_mb} MB")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise HTTPException(413, f"pictures may have at most {settings.max_upload_mb} MB")
        try:
            images.extension(bytes(data))
        except images.ImageError as exc:
            raise HTTPException(415, str(exc)) from None
        return bytes(data)

    @app.put("/api/products/{product_id}/image")
    async def put_product_image(product_id: int, request: Request) -> dict:
        # Checked before the upload is read, so nobody without a session can
        # make the server buffer a picture.
        with db.session(settings.database) as conn:
            _load_product(conn, user_dep(request, conn), product_id)
        data = await read_picture(request)
        with db.session(settings.database) as conn:
            user = user_dep(request, conn)
            row = _load_product(conn, user, product_id)
            name = images.save(settings.images, "products", data)
            conn.execute("UPDATE products SET image = ?, updated_at = ? WHERE id = ?", (name, db.now(), product_id))
            result = _product_json(conn, _load_product(conn, user, product_id))
        images.delete(settings.images, "products", row["image"])
        log.info("product_image_set", extra={"product_id": product_id, "bytes": len(data)})
        return result

    @app.delete("/api/products/{product_id}/image")
    def delete_product_image(product_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_product(conn, user, product_id)
        conn.execute("UPDATE products SET image = NULL, updated_at = ? WHERE id = ?", (db.now(), product_id))
        conn.commit()
        images.delete(settings.images, "products", row["image"])
        return _product_json(conn, _load_product(conn, user, product_id))

    # ---- ratings -----------------------------------------------------------

    @app.post("/api/products/{product_id}/ratings", status_code=201)
    def create_rating(product_id: int, body: RatingIn, user: CurrentUser, conn: Conn) -> dict:
        _load_product(conn, user, product_id)
        stamp = db.now()
        rated_on = (body.rated_on or date.today()).isoformat()
        rating_id = conn.execute(
            "INSERT INTO ratings (product_id, user_id, buy_again, stars, reason, store, rated_on, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, user.id, int(body.buy_again), body.stars, body.reason, body.store, rated_on, stamp, stamp),
        ).lastrowid
        log.info("rating_created", extra={"product_id": product_id, "rating_id": rating_id})
        return _rating(_load_rating(conn, user, rating_id))

    @app.patch("/api/ratings/{rating_id}")
    def update_rating(rating_id: int, body: RatingPatch, user: CurrentUser, conn: Conn) -> dict:
        _load_rating(conn, user, rating_id)
        changes: dict = {}
        if body.buy_again is not None:
            changes["buy_again"] = int(body.buy_again)
        if body.clear_stars:
            changes["stars"] = None
        elif body.stars is not None:
            changes["stars"] = body.stars
        if body.reason is not None:
            changes["reason"] = body.reason
        if body.store is not None:
            changes["store"] = body.store
        if body.rated_on is not None:
            changes["rated_on"] = body.rated_on.isoformat()
        if changes:
            columns = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE ratings SET {columns}, updated_at = ? WHERE id = ?",
                         [*changes.values(), db.now(), rating_id])
            log.info("rating_updated", extra={"rating_id": rating_id})
        return _rating(_load_rating(conn, user, rating_id))

    @app.delete("/api/ratings/{rating_id}", status_code=204)
    def delete_rating(rating_id: int, user: CurrentUser, conn: Conn) -> None:
        row = _load_rating(conn, user, rating_id)
        conn.execute("DELETE FROM ratings WHERE id = ?", (rating_id,))
        conn.commit()
        images.delete(settings.images, "ratings", row["photo"])
        log.info("rating_deleted", extra={"rating_id": rating_id})

    @app.put("/api/ratings/{rating_id}/photo")
    async def put_rating_photo(rating_id: int, request: Request) -> dict:
        with db.session(settings.database) as conn:
            _load_rating(conn, user_dep(request, conn), rating_id)
        data = await read_picture(request)
        with db.session(settings.database) as conn:
            user = user_dep(request, conn)
            row = _load_rating(conn, user, rating_id)
            name = images.save(settings.images, "ratings", data)
            conn.execute("UPDATE ratings SET photo = ?, updated_at = ? WHERE id = ?", (name, db.now(), rating_id))
            result = _rating(_load_rating(conn, user, rating_id))
        images.delete(settings.images, "ratings", row["photo"])
        log.info("rating_photo_set", extra={"rating_id": rating_id, "bytes": len(data)})
        return result

    @app.delete("/api/ratings/{rating_id}/photo")
    def delete_rating_photo(rating_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_rating(conn, user, rating_id)
        conn.execute("UPDATE ratings SET photo = NULL, updated_at = ? WHERE id = ?", (db.now(), rating_id))
        conn.commit()
        images.delete(settings.images, "ratings", row["photo"])
        return _rating(_load_rating(conn, user, rating_id))

    # ---- pictures and interface -------------------------------------------

    @app.get("/images/{kind}/{name}")
    def picture(kind: str, name: str, user: CurrentUser, conn: Conn) -> FileResponse:
        target = images.path(settings.images, kind, name)
        if target is None:
            raise HTTPException(404, "not found")
        # Only pictures of the user's own household.
        if kind == "products":
            owner = conn.execute(
                "SELECT 1 FROM products WHERE image = ? AND household_id = ?", (name, user.household_id)
            ).fetchone()
        else:
            owner = conn.execute(
                "SELECT 1 FROM ratings r JOIN products p ON p.id = r.product_id"
                " WHERE r.photo = ? AND p.household_id = ?",
                (name, user.household_id),
            ).fetchone()
        if owner is None or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, media_type=images.MEDIA_TYPES[target.suffix[1:]],
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def run() -> None:
    """Start the web server; the container's service calls this."""
    import uvicorn

    from . import logging_setup

    try:
        settings = config.load()
    except config.ConfigError as exc:
        logging_setup.configure("INFO")
        log.error("invalid_setting", extra={"detail": str(exc)})
        raise SystemExit(1) from None
    logging_setup.configure(settings.log_level)
    log.info(
        "starting",
        extra={
            "version": __version__,
            "port": settings.port,
            "off_lookup": settings.off_lookup,
            "off_language": settings.off_language,
            "session_days": settings.session_days,
        },
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        server_header=False,
    )

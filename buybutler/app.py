"""Web app: login, products, ratings, pictures and the interface."""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__, auth, config, db, images, lists, off, prices, shopping, twofactor

log = logging.getLogger("buybutler")

STATIC_DIR = Path(__file__).parent / "static"
SERVICE_WORKER = Path(__file__).parent / "sw.js"
# Rated products a phone keeps for offline use, at most.
OFFLINE_MAX = 5000
# Loaded only to set up an authenticator app; not worth keeping offline.
NOT_OFFLINE = {"vendor/zxing/writer.js", "vendor/zxing/zxing_writer.wasm"}
COOKIE = "buybutler_session"
# The list the phone works in; sent with every request (see list_dep).
LIST_HEADER = "x-buybutler-list"
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
    "Permissions-Policy": (
        "camera=(self), microphone=(), geolocation=(), "
        "publickey-credentials-create=(self), publickey-credentials-get=(self)"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
}


# ---------------------------------------------------------------------------
# Request and response shapes


# A random ID from the phone (a UUID); see db migration 3.
ClientId = Annotated[str | None, Field(default=None, pattern=r"^[A-Za-z0-9-]{16,64}$")]


def _text(value: str | None, limit: int) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise ValueError(f"at most {limit} characters")
    return value


class Login(BaseModel):
    username: str = Field(max_length=auth.USERNAME_MAX)
    password: str = Field(max_length=1024)


def _name(v: str, what: str = "name") -> str:
    v = _text(v, 100)
    if not v:
        raise ValueError(f"the {what} is required")
    return v


class ListIn(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _name(v)


class ListDelete(BaseModel):
    # The list's name, typed by the user to confirm.
    confirm_name: str = Field(max_length=200)


class MemberIn(BaseModel):
    username: str = Field(max_length=auth.USERNAME_MAX)
    role: Literal["owner", "editor", "viewer"] = "editor"


class MemberPatch(BaseModel):
    role: Literal["owner", "editor", "viewer"]


class UserIn(BaseModel):
    username: str = Field(max_length=auth.USERNAME_MAX)
    password: str = Field(max_length=1024)
    role: Literal["admin", "member"] = "member"


class UserPatch(BaseModel):
    role: Literal["admin", "member"]


class PasswordSet(BaseModel):
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


# A ticket names a login or passkey registration in progress (see
# twofactor.new_challenge).
Ticket = Annotated[str, Field(min_length=20, max_length=100)]


class SecondFactor(BaseModel):
    ticket: Ticket
    # A 6-digit code from the authenticator app, or a recovery code.
    code: str = Field(max_length=40)


class PasskeyStart(BaseModel):
    # The ticket of a password login waiting for its second factor; without
    # one, any passkey of this site can log in.
    ticket: Ticket | None = None


class PasskeyAnswer(BaseModel):
    ticket: Ticket
    # The browser's PublicKeyCredential as JSON; py_webauthn checks it.
    credential: dict


class PasswordConfirm(BaseModel):
    password: str = Field(max_length=1024)


class TotpConfirm(BaseModel):
    code: str = Field(max_length=20)


class PasskeyIn(PasskeyAnswer):
    name: str = "Passkey"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _text(v, twofactor.PASSKEY_NAME_MAX) or "Passkey"


class ProductIn(BaseModel):
    client_id: ClientId = None
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


def _unit(v: str) -> str:
    return _text(v, 20)


class ShoppingIn(BaseModel):
    client_id: ClientId = None
    product_id: int | None = None
    text: str = ""
    quantity: str = ""
    store: str = ""

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return _text(v, shopping.TEXT_MAX)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v: str) -> str:
        return _text(v, shopping.QUANTITY_MAX)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str) -> str:
        return _text(v, shopping.STORE_MAX)


class ShoppingPatch(BaseModel):
    checked: bool | None = None
    text: str | None = None
    quantity: str | None = None
    store: str | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, shopping.TEXT_MAX)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, shopping.QUANTITY_MAX)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, shopping.STORE_MAX)


class ShoppingOrder(BaseModel):
    store: str = Field(default="", max_length=shopping.STORE_MAX)
    keys: list[Annotated[str, Field(max_length=shopping.TEXT_MAX + 2)]] = Field(max_length=2000)


ItemId = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{16,64}$")]


class PriceIn(BaseModel):
    client_id: ClientId = None
    # Cents, up to 100 000.00 in the currency.
    amount_cents: int = Field(ge=0, le=10_000_000)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: str = ""
    on_offer: bool = False
    store: str = ""
    bought_on: date | None = None

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        return _unit(v)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str) -> str:
        return _text(v, 100)

    def model_post_init(self, _context) -> None:
        if self.quantity is not None and not self.unit:
            raise ValueError("a quantity needs a unit")


class PricePatch(BaseModel):
    amount_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)
    clear_quantity: bool = False
    unit: str | None = None
    on_offer: bool | None = None
    store: str | None = None
    bought_on: date | None = None

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str | None) -> str | None:
        return None if v is None else _unit(v)

    @field_validator("store")
    @classmethod
    def _store(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, 100)


class RatingIn(BaseModel):
    client_id: ClientId = None
    buy_again: bool
    stars: int | None = Field(default=None, ge=1, le=5)
    reason: str = ""
    store: str = ""
    rated_on: date | None = None
    # Optional: what it cost this time. Stored as a price entry of its own,
    # with the rating's store and date.
    price: PriceIn | None = None

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


def _product(row: sqlite3.Row, latest: sqlite3.Row | None, count: int,
             price_rows: list[sqlite3.Row] | None = None) -> dict:
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
        "price": prices.summary(price_rows or []),
        "list_id": row["list_id"],
        # The caller's role in the product's list decides what the page offers.
        "role": row["role"] if "role" in row.keys() else None,
    }


ROLES = ("owner", "editor", "viewer")
WRITE_ROLES = ("owner", "editor")


def _check_write(role: str) -> None:
    if role not in WRITE_ROLES:
        raise HTTPException(403, "this list is read only for you")


def _list_role(conn: sqlite3.Connection, user: auth.User, list_id: int) -> str | None:
    row = conn.execute(
        "SELECT role FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, user.id)
    ).fetchone()
    return row["role"] if row else None


# Every record is reached through its product, and every product through a
# list the caller is a member of; the role comes along for write checks.
MEMBER_JOIN = " JOIN list_members m ON m.list_id = p.list_id AND m.user_id = ?"


def _load_product(conn: sqlite3.Connection, user: auth.User, product_id: int, write: bool = False) -> sqlite3.Row:
    row = conn.execute(
        "SELECT p.*, m.role FROM products p" + MEMBER_JOIN + " WHERE p.id = ?", (user.id, product_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "product not found")
    if write:
        _check_write(row["role"])
    return row


def _product_json(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    latest = conn.execute(
        RATING_SELECT + " WHERE r.product_id = ? ORDER BY r.rated_on DESC, r.created_at DESC, r.id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM ratings WHERE product_id = ?", (row["id"],)).fetchone()[0]
    price_rows = conn.execute(prices.SELECT + " WHERE pr.product_id = ? " + prices.ORDER, (row["id"],)).fetchall()
    return _product(row, latest, count, price_rows)


def _load_price(conn: sqlite3.Connection, user: auth.User, price_id: int, write: bool = False) -> sqlite3.Row:
    row = conn.execute(
        prices.SELECT.replace("SELECT pr.*,", "SELECT pr.*, m.role,")
        + " JOIN products p ON p.id = pr.product_id" + MEMBER_JOIN + " WHERE pr.id = ?",
        (user.id, price_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "price not found")
    if write:
        _check_write(row["role"])
    return row


def _after_conflict(conn: sqlite3.Connection, user: auth.User, table: str, client_id: str | None) -> int | None:
    """A repeated request that ran at the same time as the first copy: its
    insert hit the unique client_id. The first copy has committed by now
    (SQLite serialises writers); start over and return its record."""
    if not client_id:
        return None
    conn.rollback()
    return _replayed(conn, user, table, client_id)


def _replayed(conn: sqlite3.Connection, user: auth.User, table: str, client_id: str | None) -> int | None:
    """The ID of a record an earlier copy of this request created, if any.

    The phone repeats a request when the answer got lost on a bad network;
    the record then exists already and is returned instead of a second one.
    """
    if not client_id:
        return None
    owner = {
        "products": "SELECT id, list_id FROM products WHERE client_id = ?",
        "ratings": "SELECT r.id, p.list_id FROM ratings r JOIN products p ON p.id = r.product_id"
                   " WHERE r.client_id = ?",
        "prices": "SELECT pr.id, p.list_id FROM prices pr JOIN products p ON p.id = pr.product_id"
                  " WHERE pr.client_id = ?",
    }[table]
    row = conn.execute(owner, (client_id,)).fetchone()
    if row is None:
        return None
    if _list_role(conn, user, row["list_id"]) is None:
        raise HTTPException(409, "this client id is in use")
    return row["id"]


def _load_rating(conn: sqlite3.Connection, user: auth.User, rating_id: int, write: bool = False) -> sqlite3.Row:
    row = conn.execute(
        RATING_SELECT.replace("SELECT r.*,", "SELECT r.*, m.role,")
        + " JOIN products p ON p.id = r.product_id" + MEMBER_JOIN + " WHERE r.id = ?",
        (user.id, rating_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "rating not found")
    if write:
        _check_write(row["role"])
    return row


# ---------------------------------------------------------------------------
# App


def service_worker() -> bytes:
    """The service worker with the files of the app shell and a hash of
    their content. Any change to a file changes the worker, so the browser
    installs the new version (and the page offers a reload)."""
    files = sorted(
        f for f in STATIC_DIR.rglob("*")
        if f.is_file() and f.relative_to(STATIC_DIR).as_posix() not in NOT_OFFLINE
    )
    digest = hashlib.sha256(__version__.encode())
    for f in files:
        digest.update(f.relative_to(STATIC_DIR).as_posix().encode())
        digest.update(f.read_bytes())
    urls = ["/", "/manifest.webmanifest"] + ["/static/" + f.relative_to(STATIC_DIR).as_posix() for f in files]
    return (
        SERVICE_WORKER.read_text()
        .replace("__VERSION__", f"{__version__}-{digest.hexdigest()[:12]}")
        .replace("__FILES__", json.dumps(urls))
        .encode()
    )


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

    # scope="function": the transaction is committed before the response
    # goes out. With the default request scope the commit runs after it, and
    # the browser's next request could miss a record it was told exists.
    Conn = Annotated[sqlite3.Connection, Depends(conn_dep, scope="function")]

    def secure(request: Request) -> bool:
        # Behind the reverse proxy, uvicorn takes the scheme from
        # X-Forwarded-Proto; a plain-HTTP test on the LAN still gets a cookie.
        return request.url.scheme == "https"

    def set_session_cookie(request: Request, response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=secure(request),
            path="/",
        )

    def session_of(request: Request, conn: sqlite3.Connection, response: Response | None = None) -> auth.User:
        token = request.cookies.get(COOKIE)
        user, extended = auth.session_user(conn, token, settings.session_days) if token else (None, False)
        if user is None:
            raise HTTPException(401, "not logged in")
        # The server extends a session while it is used; the cookie has to
        # move along, or the browser drops it session_days after the login.
        if extended and response is not None:
            set_session_cookie(request, response, token)
        return user

    def user_dep(request: Request, response: Response, conn: Conn) -> auth.User:
        return session_of(request, conn, response)

    CurrentUser = Annotated[auth.User, Depends(user_dep)]

    def list_dep(request: Request, user: CurrentUser, conn: Conn) -> tuple[int, str]:
        """The list the phone works in (header), and the caller's role there."""
        raw = request.headers.get(LIST_HEADER, "").strip()
        if raw:
            if not raw.isdigit():
                raise HTTPException(422, "the list id is a number")
            role = _list_role(conn, user, int(raw))
            if role is None:
                raise HTTPException(404, "list not found")
            return int(raw), role
        row = conn.execute(
            "SELECT list_id, role FROM list_members WHERE user_id = ? ORDER BY list_id LIMIT 1", (user.id,)
        ).fetchone()
        if row is None:
            raise HTTPException(409, "no list yet: create one first")
        return row["list_id"], row["role"]

    CurrentList = Annotated[tuple[int, str], Depends(list_dep)]

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ---- health and session ------------------------------------------------

    @app.get("/health")
    def health(conn: Conn) -> dict:
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": __version__}

    @app.post("/api/session")
    def login(body: Login, request: Request, response: Response, conn: Conn) -> dict:
        client = client_of(request)
        wait, attempt = throttle.begin(body.username, client)
        if attempt is None:
            log.warning("login_throttled", extra={"client": client})
            raise HTTPException(429, "too many failed logins, try again later", headers={"Retry-After": str(wait)})
        user = auth.authenticate(conn, body.username, body.password)
        if user is None:
            log.warning("login_failed", extra={"client": client})
            raise HTTPException(401, "wrong user name or password")
        throttle.succeeded(body.username, client, attempt)
        if twofactor.enabled(conn, user.id):
            # The password was right; the session comes after the second factor.
            ticket = new_ticket(conn, "second_factor", user.id)
            log.info("login_needs_second_factor", extra={"user_id": user.id, "client": client})
            return {"second_factor": {
                "ticket": ticket, "methods": twofactor.methods(conn, user.id, bool(settings.public_url)),
            }}
        return log_in(request, response, conn, user, "password")

    def log_in(request: Request, response: Response, conn: sqlite3.Connection, user: auth.User,
               method: str) -> dict:
        set_session_cookie(request, response, auth.start_session(conn, user.id, settings.session_days))
        log.info("login_succeeded", extra={"user_id": user.id, "client": client_of(request), "method": method})
        return me(user)

    def new_ticket(conn: sqlite3.Connection, purpose: str, user_id: int | None, challenge: bytes = b"") -> str:
        try:
            return twofactor.new_challenge(conn, purpose, user_id, challenge)
        except twofactor.TooManyChallenges:
            log.warning("too_many_challenges")
            raise HTTPException(429, "too many logins at the same time, try again later",
                                headers={"Retry-After": "60"}) from None

    def user_by_id(conn: sqlite3.Connection, user_id: int) -> auth.User | None:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        return auth.User(row["id"], row["username"], row["role"]) if row else None

    @app.post("/api/session/second-factor")
    def second_factor(body: SecondFactor, request: Request, response: Response, conn: Conn) -> dict:
        ticket = twofactor.get_challenge(conn, body.ticket, "second_factor")
        user = user_by_id(conn, ticket["user_id"]) if ticket else None
        if user is None:
            raise HTTPException(401, "the login took too long, start again")
        # The same limit as the password: 5 wrong codes lock the account for a while.
        client = client_of(request)
        wait, attempt = throttle.begin(user.username, client)
        if attempt is None:
            log.warning("login_throttled", extra={"client": client})
            raise HTTPException(429, "too many wrong codes, try again later", headers={"Retry-After": str(wait)})
        code = body.code.strip()
        if code.isdigit():
            method, accepted = "totp", twofactor.check_totp(conn, user.id, code)
        else:
            method, accepted = "recovery_code", twofactor.use_recovery_code(conn, user.id, code)
        if not accepted:
            log.warning("second_factor_failed", extra={"user_id": user.id, "client": client, "method": method})
            raise HTTPException(401, "wrong code")
        throttle.succeeded(user.username, client, attempt)
        twofactor.drop_challenge(conn, body.ticket)
        result = log_in(request, response, conn, user, method)
        if method == "recovery_code":
            result["recovery_codes_left"] = twofactor.recovery_left(conn, user.id)
        return result

    def passkeys_on() -> None:
        if not settings.public_url:
            raise HTTPException(404, "passkeys are off: BUYBUTLER_PUBLIC_URL is not set")

    @app.get("/api/session/options")
    def login_options() -> dict:
        """What the login page can offer, before anyone is logged in."""
        return {"passkey_origin": settings.public_url or None}

    @app.post("/api/session/passkey/options")
    def passkey_login_options(body: PasskeyStart, conn: Conn) -> dict:
        passkeys_on()
        if body.ticket is None:
            options, challenge = twofactor.authentication_options(conn, settings.passkey_rp_id, None)
            return {"ticket": new_ticket(conn, "passkey_login", None, challenge), "options": options}
        ticket = twofactor.get_challenge(conn, body.ticket, "second_factor")
        if ticket is None:
            raise HTTPException(401, "the login took too long, start again")
        options, challenge = twofactor.authentication_options(conn, settings.passkey_rp_id, ticket["user_id"])
        twofactor.set_challenge(conn, body.ticket, challenge)
        return {"ticket": body.ticket, "options": options}

    @app.post("/api/session/passkey")
    def passkey_login(body: PasskeyAnswer, request: Request, response: Response, conn: Conn) -> dict:
        passkeys_on()
        ticket = twofactor.get_challenge(conn, body.ticket, "passkey_login", "second_factor")
        if ticket is None or not ticket["challenge"]:
            raise HTTPException(401, "the login took too long, start again")
        client = client_of(request)
        expected = ticket["user_id"]
        # Who logs in is only known once the passkey holds, so a usernameless
        # login counts against the client address alone.
        name = user_by_id(conn, expected).username if expected is not None else None
        wait, attempt = throttle.begin(name, client)
        if attempt is None:
            log.warning("login_throttled", extra={"client": client})
            raise HTTPException(429, "too many failed logins, try again later", headers={"Retry-After": str(wait)})
        # A challenge is answered once, right or wrong.
        twofactor.drop_challenge(conn, body.ticket)
        user_id = twofactor.authenticate(conn, settings.passkey_rp_id, settings.public_url, body.credential,
                                         twofactor.challenge_of(ticket), expected)
        user = user_by_id(conn, user_id) if user_id is not None else None
        if user is None:
            conn.commit()
            log.warning("passkey_failed", extra={"client": client})
            raise HTTPException(401, "the passkey was not accepted")
        throttle.succeeded(name, client, attempt)
        return log_in(request, response, conn, user, "passkey")

    @app.delete("/api/session", status_code=204)
    def logout(request: Request, response: Response, conn: Conn) -> None:
        token = request.cookies.get(COOKIE)
        if token:
            auth.end_session(conn, token)
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/session")
    def me(user: CurrentUser) -> dict:
        return {"username": user.username, "role": user.role, "currency": settings.currency}

    def confirm_password(request: Request, conn: sqlite3.Connection, user: auth.User, password: str) -> None:
        """The current password, for changes a stolen session must not make.
        The same limit as the login: a session alone must not allow guessing it."""
        client = client_of(request)
        wait, attempt = throttle.begin(user.username, client)
        if attempt is None:
            raise HTTPException(429, "too many wrong passwords, try again later", headers={"Retry-After": str(wait)})
        if auth.authenticate(conn, user.username, password) is None:
            log.warning("password_check_failed", extra={"user_id": user.id})
            raise HTTPException(400, "the current password is wrong")
        throttle.succeeded(user.username, client, attempt)

    @app.post("/api/session/password", status_code=204)
    def change_password(body: PasswordChange, user: CurrentUser, request: Request, response: Response,
                        conn: Conn) -> None:
        confirm_password(request, conn, user, body.current)
        try:
            auth.set_password(conn, user.id, body.new)
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from None
        # Every session ended with the change; this one continues with a new token.
        set_session_cookie(request, response, auth.start_session(conn, user.id, settings.session_days))
        log.info("password_changed", extra={"user_id": user.id})

    # ---- second factors of the logged-in user ---------------------------------

    def security_json(conn: sqlite3.Connection, user: auth.User, codes: list[str] | None = None) -> dict:
        result = twofactor.status(conn, user.id)
        result["passkey_origin"] = settings.public_url or None
        if codes is not None:
            result["recovery_codes"] = codes
        return result

    def changed_second_factor(request: Request, response: Response, conn: sqlite3.Connection,
                              user: auth.User, was_on: bool) -> list[str] | None:
        """After a second factor was added: when two-factor login just
        turned on, new recovery codes, and every other session ends (someone
        who knew the password may have logged in before)."""
        if was_on:
            return None
        codes = twofactor.new_recovery_codes(conn, user.id)
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        set_session_cookie(request, response, auth.start_session(conn, user.id, settings.session_days))
        log.info("two_factor_enabled", extra={"user_id": user.id})
        return codes

    @app.get("/api/account/security")
    def get_security(user: CurrentUser, conn: Conn) -> dict:
        return security_json(conn, user)

    @app.post("/api/account/totp")
    def start_totp(body: PasswordConfirm, request: Request, user: CurrentUser, conn: Conn) -> dict:
        confirm_password(request, conn, user, body.password)
        try:
            secret = twofactor.start_totp(conn, user.id)
        except twofactor.TwoFactorError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"secret": secret, "uri": twofactor.provisioning_uri(secret, user.username)}

    @app.post("/api/account/totp/confirm")
    def confirm_totp(body: TotpConfirm, request: Request, response: Response, user: CurrentUser,
                     conn: Conn) -> dict:
        client = client_of(request)
        wait, attempt = throttle.begin(user.username, client)
        if attempt is None:
            raise HTTPException(429, "too many wrong codes, try again later", headers={"Retry-After": str(wait)})
        was_on = twofactor.enabled(conn, user.id)
        if not twofactor.check_totp(conn, user.id, body.code, confirming=True):
            raise HTTPException(400, "wrong code: check the time on the phone and try the next one")
        throttle.succeeded(user.username, client, attempt)
        log.info("totp_added", extra={"user_id": user.id})
        return security_json(conn, user, changed_second_factor(request, response, conn, user, was_on))

    @app.delete("/api/account/totp")
    def remove_totp(body: PasswordConfirm, request: Request, user: CurrentUser, conn: Conn) -> dict:
        confirm_password(request, conn, user, body.password)
        twofactor.remove_totp(conn, user.id)
        log.info("totp_removed", extra={"user_id": user.id})
        return security_json(conn, user)

    @app.post("/api/account/passkeys/options")
    def passkey_register_options(body: PasswordConfirm, request: Request, user: CurrentUser, conn: Conn) -> dict:
        passkeys_on()
        confirm_password(request, conn, user, body.password)
        options, challenge = twofactor.registration_options(conn, settings.passkey_rp_id, user.id, user.username)
        return {"ticket": new_ticket(conn, "passkey_register", user.id, challenge), "options": options}

    @app.post("/api/account/passkeys", status_code=201)
    def add_passkey(body: PasskeyIn, request: Request, response: Response, user: CurrentUser,
                    conn: Conn) -> dict:
        passkeys_on()
        ticket = twofactor.get_challenge(conn, body.ticket, "passkey_register")
        if ticket is None or ticket["user_id"] != user.id:
            raise HTTPException(400, "this took too long, start again")
        twofactor.drop_challenge(conn, body.ticket)
        was_on = twofactor.enabled(conn, user.id)
        try:
            passkey_id = twofactor.register(conn, settings.passkey_rp_id, settings.public_url, user.id,
                                            body.credential, twofactor.challenge_of(ticket), body.name)
        except twofactor.TwoFactorError as exc:
            conn.commit()
            raise HTTPException(400, str(exc)) from None
        log.info("passkey_added", extra={"user_id": user.id, "passkey_id": passkey_id})
        return security_json(conn, user, changed_second_factor(request, response, conn, user, was_on))

    @app.delete("/api/account/passkeys/{passkey_id}")
    def remove_passkey(passkey_id: int, body: PasswordConfirm, request: Request, user: CurrentUser,
                       conn: Conn) -> dict:
        confirm_password(request, conn, user, body.password)
        if not twofactor.remove_passkey(conn, user.id, passkey_id):
            raise HTTPException(404, "passkey not found")
        log.info("passkey_removed", extra={"user_id": user.id, "passkey_id": passkey_id})
        return security_json(conn, user)

    @app.post("/api/account/recovery-codes")
    def new_recovery_codes(body: PasswordConfirm, request: Request, user: CurrentUser, conn: Conn) -> dict:
        confirm_password(request, conn, user, body.password)
        if not twofactor.enabled(conn, user.id):
            raise HTTPException(409, "recovery codes come with an authenticator app or a passkey")
        log.info("recovery_codes_renewed", extra={"user_id": user.id})
        return security_json(conn, user, twofactor.new_recovery_codes(conn, user.id))

    # ---- lists ---------------------------------------------------------------

    def list_json(conn: sqlite3.Connection, list_id: int, role: str) -> dict:
        row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "role": role,
            "members": conn.execute(
                "SELECT COUNT(*) FROM list_members WHERE list_id = ?", (list_id,)
            ).fetchone()[0],
            "products": conn.execute("SELECT COUNT(*) FROM products WHERE list_id = ?", (list_id,)).fetchone()[0],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def member_role(conn: sqlite3.Connection, user: auth.User, list_id: int, owner: bool = False) -> str:
        role = _list_role(conn, user, list_id)
        if role is None:
            raise HTTPException(404, "list not found")
        if owner and role != "owner":
            raise HTTPException(403, "only an owner of the list can do that")
        return role

    @app.get("/api/lists")
    def get_lists(user: CurrentUser, conn: Conn) -> dict:
        rows = conn.execute(
            "SELECT l.id, m.role FROM lists l JOIN list_members m ON m.list_id = l.id AND m.user_id = ?"
            " ORDER BY l.name COLLATE NOCASE, l.id",
            (user.id,),
        ).fetchall()
        return {"lists": [list_json(conn, r["id"], r["role"]) for r in rows]}

    @app.post("/api/lists", status_code=201)
    def create_list(body: ListIn, user: CurrentUser, conn: Conn) -> dict:
        list_id = auth.create_list(conn, user.id, body.name)
        log.info("list_created", extra={"list_id": list_id})
        return list_json(conn, list_id, "owner")

    @app.patch("/api/lists/{list_id}")
    def rename_list(list_id: int, body: ListIn, user: CurrentUser, conn: Conn) -> dict:
        member_role(conn, user, list_id, owner=True)
        conn.execute("UPDATE lists SET name = ?, updated_at = ? WHERE id = ?", (body.name, db.now(), list_id))
        return list_json(conn, list_id, "owner")

    @app.delete("/api/lists/{list_id}", status_code=204)
    def delete_list(list_id: int, body: ListDelete, user: CurrentUser, conn: Conn) -> None:
        member_role(conn, user, list_id, owner=True)
        name = conn.execute("SELECT name FROM lists WHERE id = ?", (list_id,)).fetchone()[0]
        if body.confirm_name.strip() != name:
            raise HTTPException(400, "type the list's name exactly to delete it")
        files = lists.delete_list(conn, list_id)
        conn.commit()
        lists.remove_files(settings.images, files)
        log.info("list_deleted", extra={"list_id": list_id, "pictures": len(files)})

    @app.get("/api/lists/{list_id}/members")
    def get_members(list_id: int, user: CurrentUser, conn: Conn) -> dict:
        member_role(conn, user, list_id)
        rows = conn.execute(
            "SELECT u.id, u.username, m.role, m.created_at, m.updated_at FROM list_members m"
            " JOIN users u ON u.id = m.user_id WHERE m.list_id = ?"
            " ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, u.username COLLATE NOCASE",
            (list_id,),
        ).fetchall()
        return {"members": [
            {"user_id": r["id"], "username": r["username"], "role": r["role"], "you": r["id"] == user.id,
             "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows
        ]}

    @app.post("/api/lists/{list_id}/members", status_code=201)
    def add_member(list_id: int, body: MemberIn, user: CurrentUser, conn: Conn) -> dict:
        member_role(conn, user, list_id, owner=True)
        other = conn.execute("SELECT id FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
        if other is None:
            raise HTTPException(404, "there is no user with this name")
        if conn.execute(
            "SELECT 1 FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, other["id"])
        ).fetchone():
            raise HTTPException(409, "this user is a member already")
        stamp = db.now()
        conn.execute(
            "INSERT INTO list_members (list_id, user_id, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (list_id, other["id"], body.role, stamp, stamp),
        )
        log.info("member_added", extra={"list_id": list_id, "user_id": other["id"], "role": body.role})
        return get_members(list_id, user, conn)

    @app.patch("/api/lists/{list_id}/members/{user_id}")
    def change_member(list_id: int, user_id: int, body: MemberPatch, user: CurrentUser, conn: Conn) -> dict:
        member_role(conn, user, list_id, owner=True)
        try:
            lists.set_member_role(conn, list_id, user_id, body.role)
        except lists.ListError as exc:
            raise HTTPException(409, str(exc)) from None
        log.info("member_changed", extra={"list_id": list_id, "user_id": user_id, "role": body.role})
        return get_members(list_id, user, conn)

    @app.delete("/api/lists/{list_id}/members/{user_id}", status_code=204)
    def remove_member(list_id: int, user_id: int, user: CurrentUser, conn: Conn) -> None:
        # Owners remove anyone; everyone may leave on their own.
        member_role(conn, user, list_id, owner=user_id != user.id)
        try:
            lists.remove_member(conn, list_id, user_id)
        except lists.ListError as exc:
            raise HTTPException(409, str(exc)) from None
        log.info("member_removed", extra={"list_id": list_id, "user_id": user_id})

    # ---- users (admins) ------------------------------------------------------

    def admin_only(user: auth.User) -> None:
        if user.role != "admin":
            raise HTTPException(403, "only an admin can manage users")

    def users_json(conn: sqlite3.Connection) -> dict:
        rows = conn.execute(
            "SELECT u.id, u.username, u.role, u.created_at, u.updated_at,"
            " (SELECT COUNT(*) FROM list_members m WHERE m.user_id = u.id) AS lists"
            " FROM users u ORDER BY u.username COLLATE NOCASE"
        ).fetchall()
        return {"users": [dict(r) | {"two_factor": twofactor.enabled(conn, r["id"])} for r in rows]}

    @app.get("/api/users")
    def get_users(user: CurrentUser, conn: Conn) -> dict:
        admin_only(user)
        return users_json(conn)

    @app.post("/api/users", status_code=201)
    def add_user(body: UserIn, user: CurrentUser, conn: Conn) -> dict:
        admin_only(user)
        try:
            created = auth.create_user(conn, body.username, body.password, body.role)
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("user_created", extra={"user_id": created.id, "role": created.role})
        return users_json(conn)

    @app.patch("/api/users/{user_id}")
    def change_user(user_id: int, body: UserPatch, user: CurrentUser, conn: Conn) -> dict:
        admin_only(user)
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "user not found")
        if row["role"] == "admin" and body.role != "admin" and conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 1:
            raise HTTPException(409, "the last admin can't lose the role")
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (body.role, db.now(), user_id))
        log.info("user_changed", extra={"user_id": user_id, "role": body.role})
        return users_json(conn)

    @app.post("/api/users/{user_id}/password", status_code=204)
    def reset_password(user_id: int, body: PasswordSet, user: CurrentUser, conn: Conn) -> None:
        admin_only(user)
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise HTTPException(404, "user not found")
        try:
            auth.set_password(conn, user_id, body.password)
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("password_reset", extra={"user_id": user_id})

    @app.post("/api/users/{user_id}/reset-2fa")
    def reset_two_factor(user_id: int, user: CurrentUser, conn: Conn) -> dict:
        """For a user who lost their phone and their recovery codes."""
        admin_only(user)
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise HTTPException(404, "user not found")
        twofactor.reset(conn, user_id)
        log.info("two_factor_reset", extra={"user_id": user_id})
        return users_json(conn)

    @app.delete("/api/users/{user_id}", status_code=204)
    def delete_user(user_id: int, user: CurrentUser, conn: Conn) -> None:
        admin_only(user)
        if user_id == user.id:
            raise HTTPException(409, "you can't delete yourself")
        try:
            files = lists.delete_user(conn, user_id)
        except lists.ListError as exc:
            raise HTTPException(409 if "no such" not in str(exc) else 404, str(exc)) from None
        conn.commit()
        lists.remove_files(settings.images, files)
        log.info("user_deleted", extra={"user_id": user_id})

    # ---- lookup ------------------------------------------------------------

    @app.get("/api/lookup")
    def lookup(user: CurrentUser, current: CurrentList, conn: Conn, barcode: str = Query(max_length=14)) -> dict:
        barcode = barcode.strip()
        if not off.valid_barcode(barcode):
            raise HTTPException(422, "a barcode has 8 to 14 digits")
        row = conn.execute(
            "SELECT p.*, ? AS role FROM products p WHERE list_id = ? AND barcode = ?",
            (current[1], current[0], barcode),
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
        current: CurrentList,
        conn: Conn,
        verdict: Literal["all", "buy", "skip", "unrated"] = "all",
        q: str = Query("", max_length=200),
        store: str = Query("", max_length=100),
        category: str = Query("", max_length=100),
        sort: Literal["recent", "name", "stars", "price"] = "recent",
    ) -> dict:
        where = ["p.list_id = ?"]
        args: list = [current[0]]
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
            # By the last price paid, cheapest first; products without a price last.
            "price": "last_price IS NULL, last_price ASC, p.name COLLATE NOCASE ASC",
        }[sort]
        return {"products": product_rows(conn, current, where, args, order, 1000)}

    def product_rows(conn: sqlite3.Connection, current: tuple[int, str], where: list[str], args: list,
                     order: str, limit: int) -> list[dict]:
        rows = conn.execute(
            "SELECT p.*, ? AS role, lr.id AS lr_id,"
            " (SELECT COUNT(*) FROM ratings c WHERE c.product_id = p.id) AS rating_count,"
            " (SELECT pr.amount_cents FROM prices pr WHERE pr.product_id = p.id"
            "  ORDER BY pr.bought_on DESC, pr.created_at DESC, pr.id DESC LIMIT 1) AS last_price"
            f" FROM products p LEFT JOIN ratings lr ON lr.id = {LATEST}"
            f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}",
            [current[1], *args],
        ).fetchall()
        ids = [r["lr_id"] for r in rows if r["lr_id"] is not None]
        latest: dict[int, sqlite3.Row] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(RATING_SELECT + f" WHERE r.id IN ({marks})", ids):
                latest[r["id"]] = r
        by_product: dict[int, list[sqlite3.Row]] = {}
        product_ids = [r["id"] for r in rows if r["last_price"] is not None]
        if product_ids:
            marks = ",".join("?" * len(product_ids))
            for r in conn.execute(prices.SELECT + f" WHERE pr.product_id IN ({marks}) " + prices.ORDER, product_ids):
                by_product.setdefault(r["product_id"], []).append(r)
        return [_product(r, latest.get(r["lr_id"]), r["rating_count"], by_product.get(r["id"])) for r in rows]

    @app.get("/api/offline")
    def offline_copy(user: CurrentUser, current: CurrentList, conn: Conn) -> dict:
        """The rated products of the current list with their verdict and
        prices, kept on the phone for a shop without reception."""
        products = product_rows(conn, current, ["p.list_id = ?", "lr.id IS NOT NULL"], [current[0]],
                                "p.id", OFFLINE_MAX)
        return {"list_id": current[0], "generated_at": db.now(), "products": products}

    @app.get("/api/facets")
    def facets(user: CurrentUser, current: CurrentList, conn: Conn) -> dict:
        """Stores and categories used so far, for filters and suggestions."""
        stores = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT r.store FROM ratings r JOIN products p ON p.id = r.product_id"
                " WHERE p.list_id = ? AND r.store != '' ORDER BY r.store COLLATE NOCASE",
                (current[0],),
            )
        ]
        categories = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT category FROM products WHERE list_id = ? AND category != ''"
                " ORDER BY category COLLATE NOCASE",
                (current[0],),
            )
        ]
        return {"stores": stores, "categories": categories}

    def attach_suggested_image(product_id: int, url: str) -> None:
        """Download the Open Food Facts picture after the product was saved.

        Its image server can take ten seconds or more; in the shop, saving
        must not wait for that. A picture the user set meanwhile wins.
        """
        data = lookup_client.fetch_image(url)
        if not data:
            return
        try:
            name = images.save(settings.images, "products", data)
        except images.ImageError:
            log.warning("image_rejected", extra={"product_id": product_id})
            return
        with db.session(settings.database) as conn:
            row = conn.execute("SELECT image FROM products WHERE id = ?", (product_id,)).fetchone()
            if row is not None and row["image"] is None:
                conn.execute(
                    "UPDATE products SET image = ?, updated_at = ? WHERE id = ?", (name, db.now(), product_id)
                )
                log.info("product_image_downloaded", extra={"product_id": product_id, "bytes": len(data)})
                return
        images.delete(settings.images, "products", name)

    @app.post("/api/products", status_code=201)
    def create_product(body: ProductIn, user: CurrentUser, current: CurrentList, conn: Conn,
                       tasks: BackgroundTasks, response: Response) -> dict:
        existing = _replayed(conn, user, "products", body.client_id)
        if existing is not None:
            response.status_code = 200
            return _product_json(conn, _load_product(conn, user, existing))
        list_id, role = current
        _check_write(role)
        if body.barcode and conn.execute(
            "SELECT 1 FROM products WHERE list_id = ? AND barcode = ?", (list_id, body.barcode)
        ).fetchone():
            raise HTTPException(409, "a product with this barcode exists already")
        image_url = None
        source = "manual"
        # The switch also covers suggestions cached before it was turned off.
        if body.use_suggested_image and body.barcode and settings.off_lookup:
            suggestion = lookup_client.cached_suggestion(conn, body.barcode)
            if suggestion is not None:
                source = "openfoodfacts"
                image_url = suggestion.image_url or None
        stamp = db.now()
        try:
            product_id = conn.execute(
                "INSERT INTO products (list_id, barcode, name, brand, category, quantity, image, source,"
                " created_by, created_at, updated_at, client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (list_id, body.barcode, body.name, body.brand, body.category, body.quantity, None,
                 source, user.id, stamp, stamp, body.client_id),
            ).lastrowid
        except sqlite3.IntegrityError:
            existing = _after_conflict(conn, user, "products", body.client_id)
            if existing is None:
                raise HTTPException(409, "a product with this barcode exists already") from None
            response.status_code = 200
            return _product_json(conn, _load_product(conn, user, existing))
        log.info("product_created", extra={"product_id": product_id, "source": source})
        if image_url:
            # Runs after the response, and after this request's commit.
            tasks.add_task(attach_suggested_image, product_id, image_url)
        return _product_json(conn, _load_product(conn, user, product_id))

    @app.get("/api/products/{product_id}")
    def get_product(product_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_product(conn, user, product_id)
        data = _product_json(conn, row)
        data["prices"] = [
            prices.to_json(r)
            for r in conn.execute(prices.SELECT + " WHERE pr.product_id = ? " + prices.ORDER, (product_id,))
        ]
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
        product = _load_product(conn, user, product_id, write=True)
        changes = body.model_dump(exclude_unset=True)
        if "barcode" in changes and changes["barcode"] and conn.execute(
            "SELECT 1 FROM products WHERE list_id = ? AND barcode = ? AND id != ?",
            (product["list_id"], changes["barcode"], product_id),
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
        row = _load_product(conn, user, product_id, write=True)
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
            _load_product(conn, session_of(request, conn), product_id, write=True)
        data = await read_picture(request)
        with db.session(settings.database) as conn:
            user = session_of(request, conn)
            row = _load_product(conn, user, product_id, write=True)
            name = images.save(settings.images, "products", data)
            conn.execute("UPDATE products SET image = ?, updated_at = ? WHERE id = ?", (name, db.now(), product_id))
            result = _product_json(conn, _load_product(conn, user, product_id))
        images.delete(settings.images, "products", row["image"])
        log.info("product_image_set", extra={"product_id": product_id, "bytes": len(data)})
        return result

    @app.delete("/api/products/{product_id}/image")
    def delete_product_image(product_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_product(conn, user, product_id, write=True)
        conn.execute("UPDATE products SET image = NULL, updated_at = ? WHERE id = ?", (db.now(), product_id))
        conn.commit()
        images.delete(settings.images, "products", row["image"])
        return _product_json(conn, _load_product(conn, user, product_id))

    # ---- ratings -----------------------------------------------------------

    @app.post("/api/products/{product_id}/ratings", status_code=201)
    def create_rating(product_id: int, body: RatingIn, user: CurrentUser, conn: Conn, response: Response) -> dict:
        _load_product(conn, user, product_id, write=True)
        existing = _replayed(conn, user, "ratings", body.client_id)
        if existing is not None:
            row = _load_rating(conn, user, existing)
            if row["product_id"] != product_id:
                raise HTTPException(409, "this client id is in use")
            response.status_code = 200
            return _rating(row)
        stamp = db.now()
        rated_on = (body.rated_on or date.today()).isoformat()
        try:
            rating_id = conn.execute(
                "INSERT INTO ratings (product_id, user_id, buy_again, stars, reason, store, rated_on, created_at,"
                " updated_at, client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, user.id, int(body.buy_again), body.stars, body.reason, body.store, rated_on, stamp,
                 stamp, body.client_id),
            ).lastrowid
        except sqlite3.IntegrityError:
            existing = _after_conflict(conn, user, "ratings", body.client_id)
            if existing is None:
                raise
            response.status_code = 200
            return _rating(_load_rating(conn, user, existing))
        log.info("rating_created", extra={"product_id": product_id, "rating_id": rating_id})
        if body.price is not None:
            price = body.price.model_copy(update={"store": body.price.store or body.store,
                                                  "bought_on": body.price.bought_on or date.fromisoformat(rated_on)})
            insert_price(conn, user, product_id, price, rating_id)
        return _rating(_load_rating(conn, user, rating_id))

    # ---- prices ------------------------------------------------------------

    def insert_price(conn: sqlite3.Connection, user: auth.User, product_id: int, body: PriceIn,
                     rating_id: int | None = None) -> int:
        stamp = db.now()
        price_id = conn.execute(
            "INSERT INTO prices (product_id, user_id, rating_id, amount_cents, quantity, unit, on_offer, store,"
            " bought_on, created_at, updated_at, client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, user.id, rating_id, body.amount_cents, body.quantity, body.unit, int(body.on_offer),
             body.store, (body.bought_on or date.today()).isoformat(), stamp, stamp, body.client_id),
        ).lastrowid
        log.info("price_created", extra={"product_id": product_id, "price_id": price_id})
        return price_id

    @app.post("/api/products/{product_id}/prices", status_code=201)
    def create_price(product_id: int, body: PriceIn, user: CurrentUser, conn: Conn, response: Response) -> dict:
        _load_product(conn, user, product_id, write=True)
        existing = _replayed(conn, user, "prices", body.client_id)
        if existing is None:
            try:
                return prices.to_json(_load_price(conn, user, insert_price(conn, user, product_id, body)))
            except sqlite3.IntegrityError:
                existing = _after_conflict(conn, user, "prices", body.client_id)
                if existing is None:
                    raise
        row = _load_price(conn, user, existing)
        if row["product_id"] != product_id:
            raise HTTPException(409, "this client id is in use")
        response.status_code = 200
        return prices.to_json(row)

    @app.get("/api/products/{product_id}/prices")
    def list_prices(product_id: int, user: CurrentUser, conn: Conn) -> dict:
        _load_product(conn, user, product_id)
        rows = conn.execute(prices.SELECT + " WHERE pr.product_id = ? " + prices.ORDER, (product_id,)).fetchall()
        return {"prices": [prices.to_json(r) for r in rows]}

    @app.patch("/api/prices/{price_id}")
    def update_price(price_id: int, body: PricePatch, user: CurrentUser, conn: Conn) -> dict:
        row = _load_price(conn, user, price_id, write=True)
        changes: dict = {}
        if body.amount_cents is not None:
            changes["amount_cents"] = body.amount_cents
        if body.clear_quantity:
            changes["quantity"] = None
            changes["unit"] = ""
        else:
            if body.quantity is not None:
                changes["quantity"] = body.quantity
            if body.unit is not None:
                changes["unit"] = body.unit
        if body.on_offer is not None:
            changes["on_offer"] = int(body.on_offer)
        if body.store is not None:
            changes["store"] = body.store
        if body.bought_on is not None:
            changes["bought_on"] = body.bought_on.isoformat()
        quantity = changes.get("quantity", row["quantity"])
        unit = changes.get("unit", row["unit"])
        if quantity is not None and not unit:
            raise HTTPException(422, "a quantity needs a unit")
        if changes:
            columns = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE prices SET {columns}, updated_at = ? WHERE id = ?",
                         [*changes.values(), db.now(), price_id])
            log.info("price_updated", extra={"price_id": price_id})
        return prices.to_json(_load_price(conn, user, price_id))

    @app.delete("/api/prices/{price_id}", status_code=204)
    def delete_price(price_id: int, user: CurrentUser, conn: Conn) -> None:
        _load_price(conn, user, price_id, write=True)
        conn.execute("DELETE FROM prices WHERE id = ?", (price_id,))
        log.info("price_deleted", extra={"price_id": price_id})

    @app.patch("/api/ratings/{rating_id}")
    def update_rating(rating_id: int, body: RatingPatch, user: CurrentUser, conn: Conn) -> dict:
        _load_rating(conn, user, rating_id, write=True)
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
        row = _load_rating(conn, user, rating_id, write=True)
        conn.execute("DELETE FROM ratings WHERE id = ?", (rating_id,))
        conn.commit()
        images.delete(settings.images, "ratings", row["photo"])
        log.info("rating_deleted", extra={"rating_id": rating_id})

    @app.put("/api/ratings/{rating_id}/photo")
    async def put_rating_photo(rating_id: int, request: Request) -> dict:
        with db.session(settings.database) as conn:
            _load_rating(conn, session_of(request, conn), rating_id, write=True)
        data = await read_picture(request)
        with db.session(settings.database) as conn:
            user = session_of(request, conn)
            row = _load_rating(conn, user, rating_id, write=True)
            name = images.save(settings.images, "ratings", data)
            conn.execute("UPDATE ratings SET photo = ?, updated_at = ? WHERE id = ?", (name, db.now(), rating_id))
            result = _rating(_load_rating(conn, user, rating_id))
        images.delete(settings.images, "ratings", row["photo"])
        log.info("rating_photo_set", extra={"rating_id": rating_id, "bytes": len(data)})
        return result

    @app.delete("/api/ratings/{rating_id}/photo")
    def delete_rating_photo(rating_id: int, user: CurrentUser, conn: Conn) -> dict:
        row = _load_rating(conn, user, rating_id, write=True)
        conn.execute("UPDATE ratings SET photo = NULL, updated_at = ? WHERE id = ?", (db.now(), rating_id))
        conn.commit()
        images.delete(settings.images, "ratings", row["photo"])
        return _rating(_load_rating(conn, user, rating_id))

    # ---- shopping list -----------------------------------------------------

    def shopping_json(conn: sqlite3.Connection, list_id: int, store: str) -> dict:
        return {
            "list_id": list_id,
            "store": store,
            "stores": shopping.stores(conn, list_id),
            "items": shopping.items(conn, list_id, store, _image_url),
        }

    @app.get("/api/shopping")
    def get_shopping(user: CurrentUser, current: CurrentList, conn: Conn,
                     store: str = Query("", max_length=shopping.STORE_MAX)) -> dict:
        return shopping_json(conn, current[0], store.strip())

    @app.post("/api/shopping", status_code=201)
    def add_shopping(body: ShoppingIn, user: CurrentUser, current: CurrentList, conn: Conn,
                     response: Response) -> dict:
        _check_write(current[1])
        try:
            client_id, created = shopping.add(conn, current[0], user.id, body.client_id, body.product_id,
                                              body.text, body.quantity, body.store)
        except shopping.ShoppingError as exc:
            raise HTTPException(404 if "not found" in str(exc) else 409 if "used" in str(exc) else 422,
                                str(exc)) from None
        if created:
            log.info("shopping_item_added", extra={"list_id": current[0], "product": body.product_id is not None})
        else:
            response.status_code = 200
        return shopping.get(conn, current[0], client_id, _image_url)

    @app.patch("/api/shopping/{item_id}")
    def change_shopping(item_id: ItemId, body: ShoppingPatch, user: CurrentUser, current: CurrentList,
                        conn: Conn) -> dict | None:
        _check_write(current[1])
        try:
            shopping.update(conn, current[0], item_id, user.id, body.model_dump(exclude_none=True))
        except LookupError:
            raise HTTPException(404, "item not found") from None
        # None: unticked while the product was on the list again, so this
        # entry went (see shopping.update).
        return shopping.get(conn, current[0], item_id, _image_url)

    @app.delete("/api/shopping/{item_id}", status_code=204)
    def remove_shopping(item_id: ItemId, user: CurrentUser, current: CurrentList, conn: Conn) -> None:
        _check_write(current[1])
        if not shopping.remove(conn, current[0], item_id):
            raise HTTPException(404, "item not found")

    @app.post("/api/shopping/clear-checked")
    def clear_shopping(user: CurrentUser, current: CurrentList, conn: Conn) -> dict:
        _check_write(current[1])
        removed = shopping.clear_checked(conn, current[0])
        log.info("shopping_cleared", extra={"list_id": current[0], "items": removed})
        return {"removed": removed}

    @app.put("/api/shopping/order", status_code=204)
    def order_shopping(body: ShoppingOrder, user: CurrentUser, current: CurrentList, conn: Conn) -> None:
        _check_write(current[1])
        shopping.set_order(conn, current[0], body.store.strip(), body.keys)

    # ---- pictures and interface -------------------------------------------

    @app.get("/images/{kind}/{name}")
    def picture(kind: str, name: str, user: CurrentUser, conn: Conn) -> FileResponse:
        target = images.path(settings.images, kind, name)
        if target is None:
            raise HTTPException(404, "not found")
        # Only pictures of lists the user is a member of.
        if kind == "products":
            owner = conn.execute(
                "SELECT 1 FROM products p" + MEMBER_JOIN + " WHERE p.image = ?", (user.id, name)
            ).fetchone()
        else:
            owner = conn.execute(
                "SELECT 1 FROM ratings r JOIN products p ON p.id = r.product_id" + MEMBER_JOIN
                + " WHERE r.photo = ?",
                (user.id, name),
            ).fetchone()
        if owner is None or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, media_type=images.MEDIA_TYPES[target.suffix[1:]],
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    worker = service_worker()

    @app.get("/sw.js")
    def sw() -> Response:
        return Response(worker, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

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

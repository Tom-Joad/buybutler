# CLAUDE.md

Working agreement for this repository.

## What the project is

A self-hosted web app that remembers which groceries to buy again and which
not: barcode scan in the browser, verdict (buy again / don't buy again),
optional 1 to 5 stars, reason, store, date, photo, with a history per
product. Python (FastAPI, SQLite) on linuxserver.io's Alpine base, an
interface in plain HTML/CSS/JS without a build step.

## Hard rules

**Every row has time stamps.** `created_at` and `updated_at` (UTC, ISO 8601
with milliseconds, from `db.now()`) on every table that holds user data. The
owner wants them kept for later use; never drop or overwrite them.

**Every record is reached through a list membership.** Products belong to a
list, ratings and prices to a product, and every query joins
`list_members` for the caller (`MEMBER_JOIN`, `list_dep`). Writes check the
role (`write=True`, `_check_write`): viewers read only. A list keeps at
least one owner, the installation at least one admin (`lists.py`). Tests
cover every role on every endpoint that writes.

**Schema changes are migrations.** Append a step to `db.MIGRATIONS`; never
edit a released step.

**Nothing from a CDN.** Fonts, the barcode decoder and every other file are
served by the app. The CSP (`script-src 'self'`) enforces it.

**No personal data in logs.** IDs, counts and timings only; never product
names, reasons, user names, barcodes, passwords, codes or tickets.

**Second factors are checked on the server only.** With an authenticator
app or a passkey, `POST /api/session` returns a ticket instead of a session;
the session comes from `/api/session/second-factor` or
`/api/session/passkey`. A TOTP step and a recovery code work once, a
challenge is answered once. Changes to second factors need the current
password (`confirm_password`). Tests in `tests/test_twofactor.py` use a
software authenticator, so py_webauthn checks real signatures.

**The offline copy is for reading.** `/api/offline` sends the rated
products of the current list; the page keeps them in IndexedDB (`known`)
and the service worker (`buybutler/sw.js`, served at `/sw.js`) keeps only
the app shell and pictures, never API answers. Logging out and a `401`
clear both (`forgetOffline`). Writes offline go through the outbox only.

## Conventions

* Everything in the repository is in English.
* Few dependencies: `fastapi`, `uvicorn`, `httpx`, `webauthn` (passkeys);
  SQLite, scrypt and TOTP from the standard library. The browser shrinks
  photos, so there is no image library on the server. The QR code for the
  authenticator app comes from the zxing-wasm writer in the browser.
* The interface follows the Tom Joad Media design (`static/css/tokens.css`,
  `base.css`). Verdicts use the status colours with a word; red only for
  the wordmark's stop and the one primary button per view.
* User text is inserted with `textContent`, never `innerHTML`.
* Every request that changes data carries `X-Buybutler: 1`.
* Money is integer cents, rounded half up (`Decimal`), never floats; the
  currency comes from `BUYBUTLER_CURRENCY`.

## Testing

`python -m pytest`, then `docker build -t buybutler:test . && tests/smoke.sh
buybutler:test`. The interface was checked end to end with Playwright at
phone size, including the camera with a fake video device; repeat that for
interface changes.

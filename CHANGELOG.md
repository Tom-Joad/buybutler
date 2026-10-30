# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). From 1.0.0 on, a change that
needs action when upgrading (a renamed or removed setting, a changed API for
scripts, a different folder layout) only comes with a new major version.
Before 1.0.0, a minor version could need action; its entry says so.

## [1.0.6] - 2026-10-30

### Changed
- Updated dependencies:
  - pyasn1 from 0.6.4 to 0.6.5
  - fastapi from 0.151.0 to 0.162.0
  - starlette from 1.9.0 to 1.10.0
  - linuxserver/baseimage-alpine from `7a59ee7` to `52106d1`
  - pycparser from 3.11 to 3.12
  - httpx2 from 2.15.1 to 2.16.0

## [1.0.5] - 2026-10-28

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `python3` 3.14.10-r1

## [1.0.4] - 2026-10-27

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `python3` 3.14.10-r0 (CVE-2026-114583, CVE-2026-115557, CVE-2026-12345)

## [1.0.3] - 2026-10-26

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `curl` 8.23.0-r0 (CVE-2026-92392)
  - `libcurl` 8.23.0-r0 (CVE-2026-92392)

## [1.0.2] - 2026-10-23

### Changed
- Updated dependencies:
  - linuxserver/baseimage-alpine from `b661ece` to `7a59ee7`
  - fastapi from 0.143.3 to 0.151.0
  - zxing-wasm from 3.1.5 to 3.1.6

## [1.0.1] - 2026-10-22

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `musl` 1.2.6-r3 (CVE-2026-107891)

## [1.0.0] - 2026-10-21

The first stable release. Nothing to do when upgrading from 0.13.0.

### Added
- **arm64 image** (#25): the same image runs on x86-64 and on arm64
  machines such as a Raspberry Pi 4 or 5 with a 64-bit system.

### Changed
- The log shows the local time with its offset (`TZ`) instead of UTC.
- Every Python package in the image is pinned, also the ones pulled in by
  other packages, so each build installs the same versions.

### Fixed
- Entries waiting on a phone belong to the user who made them: when someone
  else logs in on the same phone, they are neither shown nor sent with that
  user's login, and go out once their user is back.
- An admin who sets their own password under **Account → Users** stays
  logged in on this device, as with **Change password**.
- An unexpected answer from Open Food Facts counts as a failed lookup
  instead of an error page.

## [0.13.0] - 2026-10-21

### Added
- **Add many** (#24) on the Scan view: each barcode shows a card with OK and
  Skip, and after OK the camera goes on to the next product at once. The
  server looks the barcode up in Open Food Facts and saves the product
  unrated (new endpoint `POST /api/products/scan`); a barcode Open Food Facts
  doesn't know is saved with the barcode as its name. The round's list shows
  what came back, with links to rate or name each product. Barcodes the
  list has already are not added twice; without reception the products wait
  in the outbox.

## [0.12.0] - 2026-10-21

### Added
- **Scan into the shopping list** (#23): **Scan barcodes** on the shopping
  list keeps the camera open for one product after the other. A product of
  the list goes on at once ("Don't buy again" asks first); one that is on
  the list already offers **Tick off** and **Raise quantity**; an unknown
  barcode opens the form for a new product, which goes on the shopping list
  once saved, and the camera comes back. Works without reception through the
  outbox, like adding by name.

## [0.11.0] - 2026-10-20

**Needs action when upgrading** only for scripts that call the user
endpoints (`/api/users…`): they now send `admin_password`.

### Security
- **Changes to users ask for the admin's own password:** adding a user,
  changing a role, setting a password, resetting two-factor login and
  deleting a user. A stolen admin session could take over every other
  account (remove its second factors, then set its password), and an admin
  could change their own password through the user list without the old
  one. Wrong passwords count against the login limit.

### Changed
- A start with `BUYBUTLER_TRUSTED_PROXIES=*` (the default) logs a warning
  to set the reverse proxy's address instead.
- New base image digest (`linuxserver/baseimage-alpine:3.24`).

### Fixed
- The remembered order of the shopping list keeps at most 2000 places per
  store; places of items gone long ago no longer pile up.

## [0.10.0] - 2026-10-18

### Added
- **Shopping list** (**Shop** in the top bar), one per list and shared by
  its members: add products (suggestions put "Buy again" first; "Don't buy
  again" asks and shows the reason) or any text, with an optional
  quantity; **Add to shopping** on a product page.
- Tick items off in the shop with one tap; ticked items move to "In the
  basket" with a **Price** link; **Clear ticked**.
- **Order per store:** drag the items into the order you walk through a
  store; the place of every product and text is remembered per store for
  the next time. Items can be tied to one store.
- While open, the shopping list is updated every 5 seconds. Changes work
  offline through the outbox, and the last shopping list opens offline.

### Changed
- The database is migrated on the first start (schema 6); nothing to do by
  hand.
- The top bar is a little tighter on narrow phones to fit four entries.

## [0.9.0] - 2026-10-18

### Added
- **Offline mode.** The app keeps itself on the phone and opens without a
  connection. The rated products of the current list (verdict, stars,
  reason, store, prices) are kept on the phone and refreshed while online.
- A scan shows the verdict from that copy at once, also without reception;
  the product page and the product list work from it offline, with a bar
  that says so. New ratings and prices can be added offline and are sent
  later.
- Product pictures are kept as they are viewed (the newest 300).
- "A new version is ready" with **Reload** after an update on the server.

### Changed
- Logging out deletes the offline copy and the kept pictures from the
  phone; so does a session the server ended.

## [0.8.0] - 2026-10-17

### Added
- **Two-factor login**, optional for each user, under **Account**: an
  authenticator app (TOTP, set up with a QR code) and passkeys. With one of
  them, the password alone no longer logs in.
- **Log in with a passkey**, without user name or password. Passkeys also
  serve as the second step after a password. Several per user (phone,
  laptop), each can be removed.
- Ten one-time recovery codes when two-factor login is turned on, and
  **New recovery codes**.
- Admins remove a user's second factors under **Account → Users**, or with
  `buybutler user reset-2fa <NAME>`. `user list` shows who has two-factor
  login.
- `BUYBUTLER_PUBLIC_URL`: the address Buybutler is opened at; passkeys need
  it.

### Changed
- Turning on two-factor login ends the user's other sessions. Wrong codes
  count towards the login lockout.
- The database is migrated on the first start (schema 5); nothing to do by
  hand.

## [0.7.0] - 2026-10-17

### Added
- **Several lists, shared between users.** Products, ratings and prices
  live in a list; a bar under the top bar switches between them, and the
  phone remembers the one in use.
- Sharing by user name with the roles owner, editor and viewer; members can
  leave, owners change roles and remove members. A list always keeps an
  owner. Deleting a list asks for its name.
- User management for admins under **Account → Users**: add users, change
  their role, set a new password, delete them. `buybutler user add --admin`.

### Changed
- The first start migrates the database: the household becomes the list
  "Home", its admin the owner, every other user an editor. Nothing to do
  by hand.
- New users start without a list (they create one or are added to one);
  the first user still gets "Home".
- Deleting a user also deletes the lists only they used; a shared list they
  alone own needs another owner first.
- The navigation item "Lists" is now "Products"; "Manage" in the list bar
  leads to the lists.

## [0.6.0] - 2026-10-17

### Added
- Works on a poor connection: requests without an answer (network errors,
  timeouts after 20 s, `502`/`503`/`504` from the reverse proxy) are
  repeated after 1, 3 and 8 seconds.
- New ratings, products (with their rating) and prices that still can't be
  sent are saved on the phone and sent when the connection is back. The top
  bar shows how many are waiting; **Account** lists them with **Send now**
  and **Discard**. A product saved offline whose barcode exists already gets
  the rating on the existing product.
- Scanning without a connection offers to add the product by hand.

### Fixed
- A repeated request no longer creates a product, rating or price twice:
  the phone gives every new entry an ID, and the server returns the
  existing entry for a repeat.
- "Load failed" and similar browser texts are replaced by a clear message.
- The date field no longer runs over the right edge on iPhones.

## [0.5.0] - 2026-10-17

### Added
- Scan a barcode with the phone camera, or type it; a known product shows its
  verdict at once, an unknown one is filled in from Open Food Facts.
- Ratings with **Buy again** / **Don't buy again**, optional 1 to 5 stars, a
  reason, the store, the date and a photo. Rating again keeps the history;
  the newest rating is the verdict. Ratings can be edited and deleted.
- Products without barcode, own product pictures, editing and deleting.
- Lists for **Buy again**, **Don't buy** and all products, with search
  (name, brand, category, barcode, reason) and filters for store and
  category.
- Optional purchase prices with a history per product: price, the quantity
  it was for with a free unit, an "on offer" mark, store and date. A price
  can be entered with a rating or on its own. Weights and volumes are
  converted, so prices compare per kg, litre or piece; offer prices are
  left out of the lowest and highest price. Lists show the last price and
  sort by it. Setting `BUYBUTLER_CURRENCY`.
- Time stamps on every entry: when it was made and last changed.
- Login with user name and password, sessions that last
  `BUYBUTLER_SESSION_DAYS`, password change, a lockout after five failed
  logins. Users are managed with `buybutler user add|passwd|list|delete`.
- `buybutler backup` writes a consistent copy of the database.
- Settings `BUYBUTLER_OFF_LANGUAGE`, `BUYBUTLER_OFF_LOOKUP`,
  `BUYBUTLER_SESSION_DAYS`, `BUYBUTLER_MAX_UPLOAD_MB`, `BUYBUTLER_CURRENCY`,
  `BUYBUTLER_TRUSTED_PROXIES`, `BUYBUTLER_LOG_LEVEL`, `BUYBUTLER_PORT`.
- Installable on the phone's home screen.

[1.0.6]: https://github.com/Tom-Joad/buybutler/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/Tom-Joad/buybutler/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/Tom-Joad/buybutler/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/Tom-Joad/buybutler/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/Tom-Joad/buybutler/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/Tom-Joad/buybutler/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Tom-Joad/buybutler/compare/v0.13.0...v1.0.0
[0.13.0]: https://github.com/Tom-Joad/buybutler/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Tom-Joad/buybutler/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Tom-Joad/buybutler/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Tom-Joad/buybutler/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Tom-Joad/buybutler/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Tom-Joad/buybutler/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Tom-Joad/buybutler/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Tom-Joad/buybutler/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Tom-Joad/buybutler/releases/tag/v0.5.0

# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). Before 1.0.0, a minor version may
need action when upgrading; the entry says so.

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

[0.6.0]: https://github.com/Tom-Joad/buybutler/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Tom-Joad/buybutler/releases/tag/v0.5.0

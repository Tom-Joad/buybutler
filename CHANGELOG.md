# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). Before 1.0.0, a minor version may
need action when upgrading; the entry says so.

## [0.1.0] - Unreleased

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
- Time stamps on every entry: when it was made and last changed.
- Login with user name and password, sessions that last
  `BUYBUTLER_SESSION_DAYS`, password change, a lockout after five failed
  logins. Users are managed with `buybutler user add|passwd|list|delete`.
- `buybutler backup` writes a consistent copy of the database.
- Settings `BUYBUTLER_OFF_LANGUAGE`, `BUYBUTLER_OFF_LOOKUP`,
  `BUYBUTLER_SESSION_DAYS`, `BUYBUTLER_MAX_UPLOAD_MB`,
  `BUYBUTLER_TRUSTED_PROXIES`, `BUYBUTLER_LOG_LEVEL`, `BUYBUTLER_PORT`.
- Installable on the phone's home screen.

[0.1.0]: https://github.com/Tom-Joad/buybutler/releases/tag/v0.1.0

# Buybutler

Remember which groceries to buy again and which not, and why. Scan a
product's barcode with the phone camera in the shop, and Buybutler shows your
verdict at once: **Buy again** or **Don't buy again**, with 1 to 5 stars, the
reason, the store and the date. Unknown products are filled in from
[Open Food Facts](https://world.openfoodfacts.org/). Products without a
barcode (bakery, market) are added by hand.

Every entry keeps the time it was made and last changed. Rating a product
again adds to its history instead of replacing it ("recipe changed, now too
sweet"); the newest rating is the verdict.

The container is built on linuxserver.io's Alpine image with its conventions
(`PUID`, `PGID`, `UMASK`, `TZ`, `/config`).

**The `/config` folder holds the database and every photo. Back it up, and
never delete it.**

## Supported architectures and tags

| Architecture | Available |
| :---: | :---: |
| x86-64 | ✅ |
| arm64 | ❌ |

Only x86-64 is built for now.

| Tag | Meaning |
|---|---|
| `latest` | The latest release |
| `0.1.0`, `0.1` | A specific release / the latest patch of a minor version |
| `sha-<commit>` | A specific build |
| `dev` | Test build from a branch, made by a manual run; not for regular use |

Images are built by GitHub Actions for every version tag, published to
`ghcr.io` and signed keylessly with Sigstore. The image inherits the
repository's visibility, so it is private: pulling it needs a token with
`read:packages` (`docker login ghcr.io`).

## Usage

### docker compose

```bash
cp .env.example .env      # optional settings
docker compose up -d
docker exec -it buybutler buybutler user add <NAME>
```

See [`docker-compose.yml`](docker-compose.yml).

### docker run

```bash
docker run -d \
  --name buybutler \
  -p 8080:8080 \
  --security-opt no-new-privileges \
  -e PUID=1000 -e PGID=1000 -e TZ=Europe/Berlin \
  -e BUYBUTLER_OFF_LANGUAGE=de \
  -v /path/to/config:/config \
  --restart unless-stopped \
  ghcr.io/tom-joad/buybutler:latest

docker exec -it buybutler buybutler user add <NAME>
```

### Unraid

A template and its own guide: [`unraid/README-UNRAID.md`](unraid/README-UNRAID.md).

### First start

1. Start the container. The log says `no_user_yet` until a user exists.
2. Create the first user; it becomes the admin. The password needs at least
   10 characters and is asked twice:

   ```bash
   docker exec -it buybutler buybutler user add <NAME>
   ```

3. Open Buybutler through your reverse proxy with HTTPS and log in. On the
   phone, "Add to Home Screen" installs it like an app.

**The camera needs HTTPS.** Browsers only allow camera access on HTTPS pages
(and on `localhost`). Over plain `http://<server>:8080` everything works
except scanning; type the barcode instead.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `-p 8080:8080` | -- | Web interface; point the reverse proxy here |
| `-e PUID` | `911` | User that owns the files (Unraid: `99`) |
| `-e PGID` | `911` | Group of the files (Unraid: `100`) |
| `-e UMASK` | `022` | File mode mask for new files (Unraid: `002`) |
| `-e TZ` | `Etc/UTC` | Time zone: the default date of a rating, and the log |
| `-e BUYBUTLER_OFF_LANGUAGE` | `en` | Language of product names from Open Food Facts, two letters (`de`, `fr` …) |
| `-e BUYBUTLER_OFF_LOOKUP` | `true` | `false` never sends barcodes to Open Food Facts; products are entered by hand |
| `-e BUYBUTLER_SESSION_DAYS` | `90` | Days a login stays valid without being used (1 to 3650) |
| `-e BUYBUTLER_MAX_UPLOAD_MB` | `10` | Largest accepted picture (1 to 100); the browser shrinks photos before sending |
| `-e BUYBUTLER_TRUSTED_PROXIES` | `*` | Addresses whose `X-Forwarded-*` headers are trusted. Set it to the reverse proxy's address (comma-separated list); with `*`, the client address in the log and the per-address login limit can be forged, see [Security](#security) |
| `-e BUYBUTLER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `-e BUYBUTLER_PORT` | `8080` | Port inside the container |
| `-v /config` | -- | Database, pictures, backups |

A malformed setting stops the container with an `ERROR:` line in the log.

## Paths

| Path | Content |
|---|---|
| `/config/buybutler.sqlite3` | The database: users, products, ratings, all time stamps |
| `/config/images/products/` | Product pictures (from Open Food Facts, or your own) |
| `/config/images/ratings/` | Photos attached to ratings |
| `/config/backups/` | Copies written by `buybutler backup` |

## Commands

Run them with `docker exec -it buybutler buybutler …`; they act as the
container's user, so files keep the `PUID`/`PGID` owner.

| Command | Does |
|---|---|
| `user add <NAME>` | Add a user. The first one becomes admin |
| `user passwd <NAME>` | Set a new password; that user's sessions end |
| `user list` | List users with role and creation time |
| `user delete <NAME>` | Delete a user; their ratings stay |
| `backup` | Write a consistent copy of the database to `/config/backups/` |

Several users share one household today; managing users and households in
the interface comes later. The database already records who made each
rating.

## Backups

Back up the whole `/config` folder. The database runs in WAL mode: a plain
file copy while the container runs can miss the last changes. Either stop the
container for the copy, or run `buybutler backup` first and keep the file
from `/config/backups/`. Delete old backups yourself; Buybutler doesn't.

## Privacy

- Only the barcode is sent to Open Food Facts, from the server, never from
  the phone. Answers are cached for 30 days (unknown barcodes for one day).
  `BUYBUTLER_OFF_LOOKUP=false` switches it off.
- Product pictures are downloaded once and served by Buybutler itself.
- Photos, reasons and ratings stay in `/config`. The log holds IDs and
  counts, never product names, reasons, user names or passwords.

## Security

- Log in with user name and password. Passwords are stored as scrypt hashes,
  login sessions as SHA-256 of a random token; the cookie is `HttpOnly`,
  `SameSite=Lax` and `Secure` behind HTTPS.
- Five failed logins for one account lock that account for 15 minutes, from
  any address; 20 failures from one client address lock that address. The
  account limit holds even when the client address is forged. Someone who
  knows a user name can therefore lock that account for 15 minutes on
  purpose; a restart of the container lifts every lock. Changing the
  password in the interface counts towards the same limit.
- Every change needs a request header that other sites can't set (no
  cross-site requests); a strict Content Security Policy blocks scripts from
  elsewhere.
- Put it behind a reverse proxy with HTTPS, and don't publish port 8080 to
  the internet directly.

Reporting vulnerabilities: [SECURITY.md](SECURITY.md).

## Troubleshooting

```bash
docker logs -f buybutler
```

The log is JSON lines. After the TomJoad Images banner, the first line of the
app is `starting` with the version and the settings that matter. The health
check calls `http://127.0.0.1:8080/health`.

| Symptom | Cause |
|---|---|
| "Camera needs HTTPS" | The page was opened over plain HTTP; use the reverse proxy's HTTPS address |
| "No camera access" | The browser or phone denied the camera; allow it in the site settings |
| Logged out after a password change | Intended: a new password ends every other session |
| "Too many failed logins" | Wait 15 minutes, or restart the container (also when someone else guessed wrong for your account) |
| Wrong client address in the log | `BUYBUTLER_TRUSTED_PROXIES` doesn't include the reverse proxy |

Verify the signature:

```bash
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/Tom-Joad/buybutler/\.github/workflows/build-and-push\.yml@refs/tags/v' \
  ghcr.io/tom-joad/buybutler:latest
```

## Building and testing

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest

docker build -t buybutler:test .
tests/smoke.sh buybutler:test
```

The unit tests cover the API with a temporary database and a mocked Open
Food Facts. The smoke test needs no network: it checks the settings, the
switch to `PUID:PGID`, `HOME`, `TZ`, the start banner, login, a rating, the
backup command, that data survives a restart and a re-create, and the
ordered stop.

The barcode decoder for the browser
([zxing-wasm](https://github.com/Sec-ant/zxing-wasm), MIT) is pinned in
`package.json` and copied into the image at build time; nothing loads from a
CDN. A native start (`python -m buybutler run` with `BUYBUTLER_DATA_DIR`)
works without it, except for the camera.

## Layout

| File | Content |
|---|---|
| `buybutler/app.py` | Routes: login, lookup, products, ratings, pictures |
| `buybutler/db.py` | Schema, migrations, time stamps, backup |
| `buybutler/auth.py` | Password hashing, sessions, login throttle |
| `buybutler/off.py` | Open Food Facts client and cache |
| `buybutler/images.py` | Stored pictures |
| `buybutler/static/` | Interface without a build step, stylesheets, fonts, icons |
| `root/etc/s6-overlay/` | s6 services: settings check and migration, app as `abc`, start banner |
| `root/usr/local/bin/buybutler` | The command for `docker exec` |
| `tests/` | Unit tests and the image smoke test |

Changes are listed in the [CHANGELOG](CHANGELOG.md).

# Buybutler

Remember which groceries to buy again and which not, and why. Scan a
product's barcode with the phone camera in the shop, and Buybutler shows your
verdict at once: **Buy again** or **Don't buy again**, with 1 to 5 stars, the
reason, the store and the date. Unknown products are filled in from
[Open Food Facts](https://world.openfoodfacts.org/). Products without a
barcode (bakery, market) are added by hand.

To start, or after a big shopping trip, **Add many** on the Scan view takes
one barcode after the other: confirm each with OK and go on scanning, while
Open Food Facts is asked in the background. The products are saved unrated,
to be rated later.

Optionally, note what you paid: the price, the quantity it was for
(`500 g`, `1.5 l`, `6 piece`) and whether it was an offer. Each product
keeps its price history with the price per kg, litre or piece, and the
lowest and highest regular price.

Every entry keeps the time it was made and last changed.

A shared shopping list per list: tap or scan products together (or type
anything else), tick them off in the shop, in the order you walk through it.

Optional two-factor login per user: a code from an authenticator app, or a
passkey (fingerprint or face unlock on the phone, no password typed in the
shop), with one-time recovery codes for a lost phone.

Made for the shop's poor reception: the app itself stays on the phone and
opens without a connection, and the rated products of the current list are
kept on the phone, so a scan shows the verdict at once, with or without
reception. Requests that get no answer are
repeated, and new ratings, products and prices that still can't be sent are
saved on the phone and sent when the connection is back ("2 waiting" in the
top bar, the list under **Account**). A repeated request never creates an
entry twice. Rating a product
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
| arm64 | ✅ |

One image for both: Docker picks the right one (Raspberry Pi 4 and 5 and
other arm64 machines with a 64-bit system included).

| Tag | Meaning |
|---|---|
| `latest` | The latest release |
| `1.0.0`, `1.0` | A specific release / the latest patch of a minor version |
| `sha-<commit>` | A specific build |
| `dev` | Test build from a branch, made by a manual run; not for regular use |

Images are built by GitHub Actions for every version tag, published to
`ghcr.io` and signed keylessly with Sigstore.

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

In Community Applications: search for **Buybutler** under **Apps**. More,
including the reverse proxy and backups: [`unraid/README-UNRAID.md`](unraid/README-UNRAID.md).

### First start

1. Start the container. The log says `no_user_yet` until a user exists.
2. Create the first user; it becomes the admin and gets a list "Home". The
   password needs at least 10 characters and is asked twice:

   ```bash
   docker exec -it buybutler buybutler user add <NAME>
   ```

3. Open Buybutler through your reverse proxy with HTTPS and log in. On the
   phone, "Add to Home Screen" installs it like an app.
4. More users: **Account → Users** (admins only). Share a list with them
   under **Manage → <list> → Members**.
5. For passkeys, set `BUYBUTLER_PUBLIC_URL` to the address of the reverse
   proxy, then turn on two-factor login under **Account**, see
   [Two-factor login](#two-factor-login).

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
| `-e BUYBUTLER_CURRENCY` | `EUR` | Currency of all prices, a three-letter code (`EUR`, `CHF` …); amounts are shown in the browser's number format |
| `-e BUYBUTLER_OFF_LOOKUP` | `true` | `false` never sends barcodes to Open Food Facts; products are entered by hand |
| `-e BUYBUTLER_SESSION_DAYS` | `90` | Days a login stays valid without being used (1 to 3650) |
| `-e BUYBUTLER_MAX_UPLOAD_MB` | `10` | Largest accepted picture (1 to 100); the browser shrinks photos before sending |
| `-e BUYBUTLER_PUBLIC_URL` | -- | The address Buybutler is opened at, such as `https://buybutler.example.com`. Needed for passkeys, which are bound to this host name; HTTPS and a host name (no IP address). Without it, passkeys are off and the authenticator app still works |
| `-e BUYBUTLER_TRUSTED_PROXIES` | `*` | Addresses whose `X-Forwarded-*` headers are trusted. Set it to the reverse proxy's address (comma-separated list); with `*`, the client address in the log and the per-address login limit can be forged, see [Security](#security) |
| `-e BUYBUTLER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `-e BUYBUTLER_PORT` | `8080` | Port inside the container |
| `-v /config` | -- | Database, pictures, backups |

A malformed setting stops the container with an `ERROR:` line in the log.

## Paths

| Path | Content |
|---|---|
| `/config/buybutler.sqlite3` | The database: users, products, ratings, prices, all time stamps |
| `/config/images/products/` | Product pictures (from Open Food Facts, or your own) |
| `/config/images/ratings/` | Photos attached to ratings |
| `/config/backups/` | Copies written by `buybutler backup` |

## Commands

Run them with `docker exec -it buybutler buybutler …`; they act as the
container's user, so files keep the `PUID`/`PGID` owner.

| Command | Does |
|---|---|
| `user add <NAME> [--admin]` | Add a user. The first one becomes admin and gets the list "Home"; later ones start without a list |
| `user passwd <NAME>` | Set a new password; that user's sessions end |
| `user list` | List users with role, `2fa` or `password`, and creation time |
| `user reset-2fa <NAME>` | Remove a user's authenticator app, passkeys and recovery codes, for a lost phone; the password alone logs in again |
| `user delete <NAME>` | Delete a user. Lists only they use are deleted with them; a shared list they alone own needs another owner first. Their ratings elsewhere stay, without a name |
| `backup` | Write a consistent copy of the database to `/config/backups/` |

The same is possible in the interface under **Account → Users**, for admins.
Every change there asks for the admin's own password, so a stolen admin
session alone can't reset another user's password or second factors, or
make new admins.

## Lists and sharing

Everything lives in a **list**: products, ratings, prices. Each user can
keep several (Home, Office kitchen …) and switch between them in the bar
under the top bar; the phone remembers the list in use. The same barcode can
be in several lists, each with its own verdicts.

A list is shared by name: **Manage → <list> → Members**, type the user name,
choose a role. An unknown name is reported.

| Role | Can |
|---|---|
| Owner | everything, including members, renaming and deleting the list; every list keeps at least one owner |
| Editor | add and change products, ratings and prices |
| Viewer | read only |

Members can leave a list. Deleting a list removes everything in it, for all
members, after its name is typed to confirm. Every rating shows who made it.

Users are created by admins (**Account → Users** or the `user add`
command); a new user creates their own lists or is added to one.

**Upgrading from 0.6:** the first start migrates by itself. The household
becomes the list "Home"; the admin becomes its owner, every other user an
editor.

## Two-factor login

Optional for each user, under **Account → Two-factor login**. Once a user
has an authenticator app or a passkey, the password alone no longer logs
them in. Changes here ask for the current password.

- **Authenticator app** (any app for 6-digit TOTP codes): scan the QR code,
  enter the first code to confirm. After the password, the login asks for
  the current code; each code works once.
- **Passkeys:** **Add a passkey** on each device (phone, laptop). On the
  login page, **Log in with a passkey** logs in without a user name or
  password; after a password, a passkey also serves as the second step.
  Passkeys need `BUYBUTLER_PUBLIC_URL` and work only when Buybutler is
  opened at that address.
- **Recovery codes:** ten codes are shown once when two-factor login is
  turned on. Each logs in once instead of the second step. **New recovery
  codes** replaces them.
- **Lost phone and codes:** an admin removes the user's second factors under
  **Account → Users → Reset two-factor**, or with
  `docker exec -it buybutler buybutler user reset-2fa <NAME>`.

Turning two-factor login on ends the user's other sessions. Wrong codes
count towards the same lockout as wrong passwords.

## Adding many products

Switch on **Add many** under the camera on the Scan view. Every barcode then
shows a card, **OK** or **Skip**; after OK the camera is ready for the next
product at once. The server asks Open Food Facts for name, brand, category
and picture and saves the product unrated; the round's list below the camera
fills in as the answers come back:

- **added**, with **Rate →** to rate it now (or later, from Products);
- **added, no name found**: Open Food Facts doesn't know the barcode, so the
  product carries the barcode as its name; **Name it →** opens its form;
- **in the list already**: nothing is added twice;
- **sent when the connection is back**: without reception the products wait
  on the phone, like everything else.

Typing a barcode in **Or type the barcode** works the same while **Add many**
is on.

## Shopping list

**Shop** holds one shopping list per list, shared by its members.

- **Add:** type in the field at the top. Products of the list are
  suggested, "Buy again" first; a "Don't buy again" product asks first and
  shows the reason. Anything else ("bread rolls") goes on as text. A
  quantity ("2x") is optional. On a product page, **Add to shopping** puts
  it on the list; a product is never on it twice.
- **Scan:** **Scan barcodes** opens the camera on the shopping list and
  keeps it open, so you can go through the cupboard product by product.
  A product of the list goes on at once ("Don't buy again" asks first). One
  that is on the list already offers **Tick off** and **Raise quantity**
  ("2x", "3x" …). An unknown barcode opens the form for a new product, filled
  in from Open Food Facts; once saved, it is on the shopping list and the
  camera comes back. The same barcode only counts again after it was out of
  sight for three seconds.
- **In the shop:** tap an item to tick it off; it moves to **In the
  basket**, with a **Price** link for products. **Clear ticked** removes the
  ticked items. **Edit** changes the quantity, the text, or ties an item to
  one store.
- **Order:** pick the store at the top and drag the items by **≡** into the
  order you walk through it. Buybutler remembers the place of each product
  (and each text) per store, so next time they come up in the same order;
  new items go to the end until placed once. Items tied to another store
  are folded away below.
- **Together:** while the shopping list is open, it is updated every 5
  seconds, so two people in the shop see each other's ticks. Ticking an
  item that someone ticked already keeps it ticked.
- **Without reception:** changes show at once and are sent when the
  connection is back; the last shopping list seen opens offline.

Viewers of a list see its shopping list but can't change it.

## Offline

Once opened over HTTPS, the app keeps itself on the phone (a service
worker) and opens without a connection. While online it also keeps a copy
of the **rated** products of the current list: verdict, stars, reason,
store and prices, and the shopping list as last seen. A scan looks there first, so the verdict shows at once.

Without a connection:

- a bar at the top says that the copy on the phone is shown;
- scanning, the product page (newest verdict and prices, no history) and
  the product list work from the copy;
- new ratings, prices and products, and changes to the shopping list, wait
  on the phone and are sent when the connection is back; changing or
  deleting a product needs the connection;
- an unknown barcode is "not known offline" and can be added by hand.

Product pictures are kept as they are viewed (the newest 300). **Logging
out deletes the copy and the pictures from the phone.** When a new version
of Buybutler is installed on the server, the app shows "A new version is
ready" with **Reload**.

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

- Log in with user name and password, optionally with a second factor, or
  with a passkey. Passwords are stored as scrypt hashes, login sessions and
  recovery codes as SHA-256; the cookie is `HttpOnly`, `SameSite=Lax` and
  `Secure` behind HTTPS.
- The authenticator app's secret is stored as it is in the database: an app
  has to compute the same codes, so it can't be hashed, and a key inside
  the same container would add little. Protect `/config` and its backups
  like the passwords in it.
- Passkeys require user verification (fingerprint, face or PIN) and are
  checked with py_webauthn against `BUYBUTLER_PUBLIC_URL`; a passkey made
  for another site is refused.
- Five failed logins for one account lock that account for 15 minutes, from
  any address; 20 failures from one client address lock that address. The
  account limit holds even when the client address is forged. Someone who
  knows a user name can therefore lock that account for 15 minutes on
  purpose; a restart of the container lifts every lock. Changing the
  password in the interface, and wrong second-factor codes, count towards
  the same limit.
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
| Logged out after a password change | Intended: a new password ends every other session; so does turning on two-factor login |
| No "Log in with a passkey" button | `BUYBUTLER_PUBLIC_URL` isn't set, or the page was opened at another address than that one |
| "Wrong code" from the authenticator app | The phone's clock is off, or the code was used already; wait for the next one |
| "Too many failed logins" | Wait 15 minutes, or restart the container (also when someone else guessed wrong for your account) |
| Wrong client address in the log | `BUYBUTLER_TRUSTED_PROXIES` doesn't include the reverse proxy |
| "No connection to Buybutler" | No answer after several tries (network, or the reverse proxy can't reach the container). New entries are kept on the phone and sent later |
| "1 not sent" in the top bar | The server refused a saved entry (e.g. a value it doesn't accept); **Account** shows why, with **Send now** and **Discard** |
| No offline start | The app was only opened over plain HTTP, or never opened online since the update; open it once over HTTPS |
| "A new version is ready" | Buybutler was updated on the server; **Reload** switches to it |
| Entries waiting on one phone | They live in that browser's storage; clearing the site data or a private tab drops them |
| Entries gone after someone else logged in on the phone | They belong to the user who made them and wait for them; they go out when that user logs in again |

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
| `buybutler/app.py` | Routes: login, second factors, lists, users, lookup, products, ratings, prices, shopping list, pictures |
| `buybutler/db.py` | Schema, migrations, time stamps, backup |
| `buybutler/auth.py` | Password hashing, sessions, login throttle |
| `buybutler/twofactor.py` | Authenticator app (TOTP), passkeys, recovery codes, login challenges |
| `buybutler/lists.py` | Lists, members and the rules that keep them consistent |
| `buybutler/prices.py` | Units, unit prices and the price summary |
| `buybutler/shopping.py` | Shopping list and the remembered order per store |
| `buybutler/off.py` | Open Food Facts client and cache |
| `buybutler/images.py` | Stored pictures |
| `buybutler/static/` | Interface without a build step, stylesheets, fonts, icons |
| `buybutler/sw.js` | Service worker: the app shell and pictures for offline use |
| `root/etc/s6-overlay/` | s6 services: settings check and migration, app as `abc`, start banner |
| `root/usr/local/bin/buybutler` | The command for `docker exec` |
| `tests/` | Unit tests and the image smoke test |

Changes are listed in the [CHANGELOG](CHANGELOG.md).

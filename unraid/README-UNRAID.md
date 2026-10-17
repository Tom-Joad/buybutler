# Buybutler on Unraid

## Install

The repository is private, so Unraid can't fetch the template by itself.
Copy [`Buybutler.xml`](Buybutler.xml) to the flash drive as
`/boot/config/plugins/dockerMan/templates-user/my-Buybutler.xml`, then
**Docker → Add Container → Template: Buybutler**.

The image is private as well: log in once on the Unraid console with a
GitHub token that has `read:packages`:

```bash
docker login ghcr.io -u <GITHUB-USER>
```

Check the settings and apply. Defaults that fit Unraid: `PUID=99`,
`PGID=100`, `UMASK=002`, config at `/mnt/user/appdata/buybutler`.

## First user

Open the container's console (Docker tab → Buybutler icon → Console) and
run:

```bash
buybutler user add <NAME>
```

The password is asked twice and needs at least 10 characters. This first
user is the admin and gets the list "Home"; further users are added in the
interface under **Account → Users** and get access to a list under
**Manage → <list> → Members**.

## Reverse proxy

Point the reverse proxy (Nginx Proxy Manager, SWAG, Traefik …) at
`http://<UNRAID-IP>:8080` and give it a certificate. The camera in the
browser only works over HTTPS. If the port is reachable from other hosts,
set **Trusted proxies** to the proxy's address.

## Settings Unraid users change

| Setting | Why |
|---|---|
| Product language | `de` gives German product names where Open Food Facts has them |
| Web interface port | When 8080 is taken on the server |
| TZ | Decides the date a new rating gets |

## Updates

**Docker → Check for Updates**, then apply. The database is migrated on the
first start of a new version; the log says `database_migrated`. If the
container stops after an update, the log names the cause in an `ERROR:`
line.

## Backups

The appdata folder holds the database and all photos. The Appdata Backup
plugin stops the container before copying, which gives a consistent copy.
Without it, run `buybutler backup` in the console first.

## Icon

The template names the icon `unraid/icon.png` of this repository. The
repository is private, so Unraid can't download it; copy it to the flash
drive once instead. Unraid looks there before it tries the URL:

1. Download [`unraid/icon.png`](icon.png) from the repository on
   GitHub (logged in).
2. Save it as `/boot/config/plugins/dockerMan/images/Buybutler-icon.png`, for
   example over the `flash` share:
   `\\<SERVER>\flash\config\plugins\dockerMan\images\Buybutler-icon.png`.
   The part before `-icon.png` is the container's name in Unraid, with the
   same capitals; rename the file if you named the container differently.
3. Reload the Docker tab.

The copy survives reboots and updates. If the container was renamed, copy
the file under the new name.

## Template changes

The template is not updated automatically (private repository). When
[`Buybutler.xml`](Buybutler.xml) changes, copy it to the flash drive again.

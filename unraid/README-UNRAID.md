# Buybutler on Unraid

## Install

The easy way is Community Applications: search for **Buybutler** under
**Apps** and install it. Without Community Applications, add
`https://github.com/Tom-Joad/unraid-templates` under **Docker → Add
Container → Template repositories**, then pick **Buybutler** from the
**Template** list.

Check the settings and apply. Defaults that fit Unraid: `PUID=99`,
`PGID=100`, `UMASK=002`, config at `/mnt/user/appdata/buybutler`. The image
runs on x86-64 and arm64 servers.

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

## Template changes

The template carries its `TemplateURL`, so Unraid picks up changes to it by
itself.

#!/usr/bin/env bash
# Smoke test of the image. Usage: tests/smoke.sh <image>
# Needs no network beyond Docker itself; Open Food Facts is switched off.
set -euo pipefail

IMAGE=${1:?usage: smoke.sh <image>}
WORK=$(mktemp -d)
NAME=buybutler-smoke-$$
# Use the caller's IDs so the test can edit and clean up what the container
# writes; abc must not be root.
PUID=$(id -u); PGID=$(id -g)
[[ $PUID != 0 ]] || { PUID=1000; PGID=1000; }
PASSWORD="smoke test password"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run --rm --entrypoint sh -v "$WORK:/w" "$IMAGE" -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; docker logs "$NAME" >&2 2>&1 || true; exit 1; }

# With pipefail, `docker logs | grep -q` fails at random (SIGPIPE).
in_log() { local out; out=$(docker logs "$NAME" 2>&1); grep -qE -- "$1" <<<"$out"; }

wait_healthy() {
    for _ in $(seq 1 60); do
        [[ $(docker inspect -f '{{.State.Health.Status}}' "$NAME") == healthy ]] && return 0
        sleep 2
    done
    fail "container did not become healthy"
}

start() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -e PUID="$PUID" -e PGID="$PGID" -e TZ=Europe/Berlin \
        -e BUYBUTLER_OFF_LOOKUP=false -v "$WORK/config:/config" "$IMAGE" >/dev/null
    wait_healthy
}

wait_for_exit() {
    for _ in $(seq 1 30); do
        [[ $(docker inspect -f '{{.State.Status}}' "$NAME") == running ]] || return 0
        sleep 1
    done
    return 1
}

# An HTTP request from inside the container, with a cookie jar, so the test
# needs no published port. Prints the status, then the body.
http() { # method path [json]
    docker exec -i "$NAME" python3 - "$@" <<'EOF'
import http.cookiejar, json, os, sys, urllib.request
method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else None
jar = http.cookiejar.MozillaCookieJar("/tmp/smoke-cookies")
if os.path.exists(jar.filename):
    jar.load(ignore_discard=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
req = urllib.request.Request("http://127.0.0.1:8080" + path, data=body, method=method,
                             headers={"X-Buybutler": "1", "Content-Type": "application/json"})
try:
    with opener.open(req, timeout=10) as r:
        status, kind, data = r.status, r.headers.get("Content-Type", ""), r.read()
except urllib.error.HTTPError as e:
    status, kind, data = e.code, e.headers.get("Content-Type", ""), e.read()
# Binary files (fonts, wasm) only need their status.
text = data.decode() if kind.startswith(("text/", "application/json")) else f"<{len(data)} bytes {kind}>"
jar.save(ignore_discard=True)
print(status)
print(text)
EOF
}

status_of() { head -n1 <<<"$1"; }

for bad in "BUYBUTLER_PORT=eighty" "BUYBUTLER_OFF_LOOKUP=maybe" "BUYBUTLER_SESSION_DAYS=0"; do
    echo "== $bad stops the container"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -e "$bad" "$IMAGE" >/dev/null
    wait_for_exit || fail "kept running with $bad"
    in_log "ERROR: ${bad%%=*} must be" || fail "no message about ${bad%%=*}"
done

echo "== start"
mkdir -p "$WORK/config"
start
in_log 'BASED ON IMAGES FROM LINUXSERVER\.IO' || fail "no start banner"
in_log '"event":"starting","version":"[0-9]+\.[0-9]+\.[0-9]+"' || fail "no starting line with the version"
in_log '"event":"no_user_yet"' || fail "no hint to create a user"
[[ $(stat -c %u "$WORK/config") == "$PUID" ]] || fail "/config not owned by PUID"
[[ -f $WORK/config/buybutler.sqlite3 ]] || fail "no database"
[[ $(stat -c %u "$WORK/config/buybutler.sqlite3") == "$PUID" ]] || fail "database not owned by PUID"

PS=$(docker exec "$NAME" ps -eo user,args)
USERS=$(awk '/python3 -m buybutler run/ && !/awk/ {print $1}' <<<"$PS")
[[ -n $USERS ]] || fail "service not running"
[[ $USERS != *root* ]] || fail "service runs as root: $USERS"
[[ $PS != *"busybox crond"* ]] || fail "crond runs although the app needs no cron"

PID=$(docker exec "$NAME" pgrep -f 'python3 -m buybutler run' | head -n1)
ENVIRON=$(docker exec --user abc "$NAME" sh -c "tr '\\0' '\\n' </proc/$PID/environ")
grep -qx 'HOME=/config' <<<"$ENVIRON" || fail "the service's HOME is not /config"
[[ $(docker exec "$NAME" date +%Z) == CE*T ]] || fail "TZ not applied"

echo "== interface and API"
R=$(http GET /); [[ $(status_of "$R") == 200 ]] || fail "/ returned $(status_of "$R")"
for f in /static/app.js /static/css/tokens.css /static/css/fonts/Anton-Regular-latin.woff2 \
         /static/vendor/zxing/reader.js /static/vendor/zxing/zxing_reader.wasm /manifest.webmanifest; do
    R=$(http GET "$f"); [[ $(status_of "$R") == 200 ]] || fail "$f returned $(status_of "$R")"
done
R=$(http GET /api/products); [[ $(status_of "$R") == 401 ]] || fail "API answered without login"

echo "== user, login, a rating"
OUT=$(printf '%s\n' "$PASSWORD" | docker exec -i "$NAME" buybutler user add smoke --password-stdin)
grep -q "Added smoke (admin)" <<<"$OUT" || fail "user add: $OUT"
R=$(http POST /api/session "{\"username\":\"smoke\",\"password\":\"wrong password!\"}")
[[ $(status_of "$R") == 401 ]] || fail "wrong password accepted"
R=$(http POST /api/session "{\"username\":\"smoke\",\"password\":\"$PASSWORD\"}")
[[ $(status_of "$R") == 200 ]] || fail "login failed: $R"
R=$(http POST /api/products '{"name":"Smoke bread","barcode":"4000000000001"}')
[[ $(status_of "$R") == 201 ]] || fail "product not created: $R"
R=$(http POST /api/products/1/ratings '{"buy_again":false,"stars":2,"reason":"Too dry","store":"Corner shop"}')
[[ $(status_of "$R") == 201 ]] || fail "rating not created: $R"
grep -q '"created_at":"20[0-9-]*T' <<<"$R" || fail "rating has no time stamp"
[[ -z $(find "$WORK/config" -user 0 -print -quit) ]] || fail "files in /config owned by root"
if grep -rq "$PASSWORD" "$WORK/config" 2>/dev/null; then fail "password stored in clear text"; fi

echo "== backup through the wrapper"
docker exec "$NAME" buybutler backup >/dev/null || fail "backup failed"
BACKUP=$(find "$WORK/config/backups" -name 'buybutler-*.sqlite3' | head -n1)
[[ -n $BACKUP ]] || fail "no backup file"
[[ $(stat -c %u "$BACKUP") == "$PUID" ]] || fail "backup not owned by PUID"

echo "== restart and re-create keep the data"
docker restart "$NAME" >/dev/null
wait_healthy
R=$(http GET /api/products?verdict=skip)
[[ $(status_of "$R") == 200 ]] || fail "session lost after restart: $R"
grep -q 'Smoke bread' <<<"$R" || fail "product lost after restart"
docker rm -f "$NAME" >/dev/null
start
R=$(http POST /api/session "{\"username\":\"smoke\",\"password\":\"$PASSWORD\"}")
[[ $(status_of "$R") == 200 ]] || fail "login failed after re-create"
R=$(http GET /api/products/1)
grep -q 'Too dry' <<<"$R" || fail "rating lost after re-create"

echo "== ordered stop"
docker stop -t 20 "$NAME" >/dev/null
[[ $(docker inspect -f '{{.State.ExitCode}}' "$NAME") == 0 ]] || fail "exit code not 0"
in_log '"event":"shutting down"' || fail "no shutdown line"

echo "OK"

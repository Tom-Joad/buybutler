# The barcode decoder for the browser comes from npm. This stage only copies
# its files; Node itself doesn't reach the final image.
# Pinned by digest (a multi-arch index); Dependabot proposes new digests.
FROM node:24-alpine@sha256:44fce696f38ba8c987c4db89954051000bbd05ffd8d8659ba15ed4a8602e937f AS vendor
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

# linuxserver.io's Alpine base: s6-overlay, PUID/PGID/UMASK/TZ, the abc user
# and docker mods, as in every linuxserver.io container.
# Pinned by digest (a multi-arch index); Dependabot proposes new digests.
FROM ghcr.io/linuxserver/baseimage-alpine:3.24@sha256:d2deff7a9aff1388bd79ded33a4efda6bd4b26358f9234a23deccba724738fea

# image.source is what makes the GHCR package inherit the repository's
# visibility instead of staying private on its own.
LABEL org.opencontainers.image.source="https://github.com/Tom-Joad/buybutler" \
      org.opencontainers.image.title="Buybutler" \
      org.opencontainers.image.description="Remember which groceries to buy again, and why" \
      org.opencontainers.image.licenses="MIT"

# S6_BEHAVIOUR_IF_STAGE2_FAILS: a failing init step (a malformed setting)
# stops the container instead of leaving it half up.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    BUYBUTLER_DATA_DIR=/config

# /lsiopy is linuxserver.io's place for a Python venv and already on PATH.
# pip is only needed to build the image and is removed afterwards: the
# libraries it bundles would show up in every vulnerability scan.
# apk upgrade picks up security fixes newer than the pinned base image.
# The app needs no cron, so the base image's cron service is dropped.
COPY requirements.txt /app/
RUN apk upgrade --no-cache \
 && apk add --no-cache python3 \
 && python3 -m venv /lsiopy \
 && /lsiopy/bin/pip install --no-cache-dir --requirement /app/requirements.txt \
 && /lsiopy/bin/pip uninstall --yes pip \
 && rm -f /etc/s6-overlay/s6-rc.d/user/contents.d/svc-cron

COPY buybutler/ /app/buybutler/
COPY --from=vendor /build/node_modules/zxing-wasm/dist/iife/reader/index.js /app/buybutler/static/vendor/zxing/reader.js
COPY --from=vendor /build/node_modules/zxing-wasm/dist/reader/zxing_reader.wasm /app/buybutler/static/vendor/zxing/zxing_reader.wasm
COPY --from=vendor /build/node_modules/zxing-wasm/LICENSE /app/buybutler/static/vendor/zxing/LICENSE
# s6 services (init-buybutler-config, svc-buybutler), the start banner and
# the `buybutler` command for docker exec.
COPY root/ /

# COPY keeps the source files' modes. Files at 0600 in the working copy would
# be unreadable for abc, and the app would stop at import.
RUN chmod -R a+rX /app \
 && chmod +x /etc/s6-overlay/s6-rc.d/init-buybutler-config/run \
      /etc/s6-overlay/s6-rc.d/svc-buybutler/run \
      /usr/local/bin/buybutler \
 && printf 'Buybutler version: %s\n' \
      "$(python3 -c 'import buybutler; print(buybutler.__version__)')" > /build_version

EXPOSE 8080/tcp

# /config: the database, pictures and backups.
VOLUME /config

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+(os.environ.get('BUYBUTLER_PORT') or '8080')+'/health', timeout=4).status==200 else 1)"

# The entrypoint stays the base image's /init (s6-overlay), which must run as
# PID 1: don't add `--init` or `--user` to `docker run`.

# Security policy

## Supported versions

Only the latest release receives fixes. Security fixes go into a new
release of the current version line.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them
privately through GitHub instead: on the
[Security tab](https://github.com/Tom-Joad/buybutler/security),
choose **Report a vulnerability**. You will get an answer within a few days.

## Scope notes

Buybutler holds personal data: what a household buys and thinks of it,
photos, the stores it visits and when, and the users' password hashes. Of
particular interest:

- getting at data without a valid session, or at another household's data
- session tokens or passwords ending up in logs, files or responses
- cross-site requests that change data, or script injection through
  product names, reasons or pictures
- uploads that are stored or served as something other than a picture
- requests to other hosts than Open Food Facts made by the server
- files readable by other users than `PUID`/`PGID` and `UMASK` allow

The container runs as a non-root user and listens on one TCP port (8080).
It is meant to sit behind a reverse proxy with HTTPS.

## Supply chain

Images are built by GitHub Actions from version tags only. Third-party
actions are pinned to commit SHAs, base images to digests. Python
dependencies are checked with `pip-audit`, the browser library with
`npm audit`, and the repository is scanned with `gitleaks`. Images are
scanned with Trivy and signed keylessly with cosign. To verify an image:

```bash
cosign verify ghcr.io/tom-joad/buybutler:latest \
  --certificate-identity-regexp '^https://github.com/Tom-Joad/buybutler/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

"""Stored pictures: product images and the photos attached to ratings.

Files are named by a random ID, never by anything the user typed, and only
JPEG, PNG and WebP are accepted, recognised by their first bytes. The
browser shrinks photos before sending them, so the server needs no image
library.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

KINDS = ("products", "ratings")

_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)

NAME = re.compile(r"^[A-Za-z0-9_-]{22}\.(jpg|png|webp)$")

MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


class ImageError(ValueError):
    """Data that isn't a picture this app stores."""


def extension(data: bytes) -> str:
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ImageError("only JPEG, PNG and WebP pictures are accepted")


def save(root: Path, kind: str, data: bytes) -> str:
    """Store a picture and return its file name."""
    if kind not in KINDS:
        raise ValueError(kind)
    name = f"{secrets.token_urlsafe(16)}.{extension(data)}"
    folder = root / kind
    folder.mkdir(parents=True, exist_ok=True)
    # Written next to the target and renamed, so a crash leaves no half file
    # under a name the database points to.
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)
    return name


def path(root: Path, kind: str, name: str) -> Path | None:
    if kind not in KINDS or not NAME.match(name):
        return None
    return root / kind / name


def delete(root: Path, kind: str, name: str | None) -> None:
    if not name:
        return
    target = path(root, kind, name)
    if target is not None:
        target.unlink(missing_ok=True)

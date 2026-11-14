"""Command line: the web server and the user, backup and export commands.

In the container: `docker exec -it buybutler buybutler user add <name>`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, auth, config, db, export, lists, twofactor


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat: "):
        raise auth.AuthError("the passwords don't match")
    return first


def _user(conn, name: str):
    row = conn.execute("SELECT id, username, role FROM users WHERE username = ?", (name.strip(),)).fetchone()
    if row is None:
        raise auth.AuthError(f"no user named {name!r}")
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buybutler", description="Remember which groceries to buy again.")
    parser.add_argument("--version", action="version", version=f"buybutler {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the web server")

    users = sub.add_parser("user", help="manage users").add_subparsers(dest="action", required=True)
    for action, text in (
        ("add", "add a user (the first one becomes admin and gets the list Home)"),
        ("passwd", "set a new password"),
    ):
        p = users.add_parser(action, help=text)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
        if action == "add":
            p.add_argument("--admin", action="store_true", help="may manage users")
    users.add_parser("list", help="list users")
    p = users.add_parser("reset-2fa", help="remove the authenticator app, passkeys and recovery codes (lost phone)")
    p.add_argument("name")
    p = users.add_parser("delete", help="delete a user; lists only they use go too, their ratings elsewhere stay")
    p.add_argument("name")
    keep = p.add_mutually_exclusive_group()
    keep.add_argument("--keep-name", action="store_true", help="their name stays on those ratings and prices")
    keep.add_argument("--clear-name", action="store_true", help="those ratings and prices lose the name")

    p = sub.add_parser("backup", help="write a consistent copy of the database to /config/backups")
    p.add_argument("--output", help="target file instead of the backups folder")

    sub.add_parser("export", help="write the readable copy of all lists and products to /config/export now")

    args = parser.parse_args(argv)

    if args.command == "run":
        from .app import run

        run()
        return 0

    try:
        settings = config.load()
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    db.migrate(settings.database)

    try:
        if args.command == "user":
            with db.session(settings.database) as conn:
                if args.action == "add":
                    user = auth.create_user(conn, args.name, _password(args), "admin" if args.admin else None)
                    print(f"Added {user.username} ({user.role}).")
                elif args.action == "passwd":
                    row = _user(conn, args.name)
                    auth.set_password(conn, row["id"], _password(args))
                    print(f"Password of {row['username']} changed; their sessions and access keys have ended.")
                elif args.action == "list":
                    rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY id").fetchall()
                    if not rows:
                        print("No users yet. Add one with: buybutler user add <name>")
                    for r in rows:
                        second = "2fa" if twofactor.enabled(conn, r["id"]) else "password"
                        print(f"{r['username']}\t{r['role']}\t{second}\t{r['created_at']}")
                elif args.action == "reset-2fa":
                    row = _user(conn, args.name)
                    twofactor.reset(conn, row["id"])
                    print(f"Two-factor login of {row['username']} removed; the password alone logs in again.")
                elif args.action == "delete":
                    row = _user(conn, args.name)
                    keep_name = args.keep_name
                    if not (args.keep_name or args.clear_name):
                        # Asked at a terminal; a script gets what it got
                        # before the question existed.
                        keep_name = sys.stdin.isatty() and input(
                            f"Keep the name {row['username']} on their ratings and prices in lists that stay? [y/N] "
                        ).strip().lower() in ("y", "yes")
                    try:
                        files = lists.delete_user(conn, row["id"], keep_name)
                    except lists.ListError as exc:
                        raise auth.AuthError(str(exc)) from None
                    conn.commit()
                    lists.remove_files(settings.images, files)
                    print(f"Deleted {row['username']}; their name was {'kept' if keep_name else 'removed'}"
                          " on ratings and prices in lists that stay.")
        elif args.command == "backup":
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            target = Path(args.output) if args.output else settings.backups / f"buybutler-{stamp}.sqlite3"
            db.backup(settings.database, target)
            print(f"Database written to {target}. Pictures are in {settings.images}.")
            if settings.export:
                # So a copy of /config taken now has the readable files of
                # what the database holds.
                export.Export(settings).run()
        elif args.command == "export":
            copy = export.Export(settings)
            copy.prepare()
            done = copy.run(everything=True)
            print(f"Readable copy in {settings.export_dir}: {done['products']} products and {done['lists']} lists"
                  f" written, {done['removed']} deleted ones moved to {settings.trash}; the rest was up to date.")
    except auth.AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

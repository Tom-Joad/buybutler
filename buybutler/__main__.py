"""Command line: the web server and the user and backup commands.

In the container: `docker exec -it buybutler buybutler user add <name>`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, auth, config, db


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
    for action, text in (("add", "add a user (the first one becomes admin)"), ("passwd", "set a new password")):
        p = users.add_parser(action, help=text)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    users.add_parser("list", help="list users")
    p = users.add_parser("delete", help="delete a user; their ratings stay")
    p.add_argument("name")

    p = sub.add_parser("backup", help="write a consistent copy of the database to /config/backups")
    p.add_argument("--output", help="target file instead of the backups folder")

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
                    user = auth.create_user(conn, args.name, _password(args))
                    print(f"Added {user.username} ({user.role}).")
                elif args.action == "passwd":
                    row = _user(conn, args.name)
                    auth.set_password(conn, row["id"], _password(args))
                    print(f"Password of {row['username']} changed; their sessions have ended.")
                elif args.action == "list":
                    rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY id").fetchall()
                    if not rows:
                        print("No users yet. Add one with: buybutler user add <name>")
                    for r in rows:
                        print(f"{r['username']}\t{r['role']}\t{r['created_at']}")
                elif args.action == "delete":
                    row = _user(conn, args.name)
                    admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
                    if row["role"] == "admin" and admins == 1:
                        raise auth.AuthError("the last admin can't be deleted")
                    conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
                    print(f"Deleted {row['username']}.")
        elif args.command == "backup":
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            target = Path(args.output) if args.output else settings.backups / f"buybutler-{stamp}.sqlite3"
            db.backup(settings.database, target)
            print(f"Database written to {target}. Pictures are in {settings.images}.")
    except auth.AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

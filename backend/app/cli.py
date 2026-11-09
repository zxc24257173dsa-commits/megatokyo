"""Command-line administration.

    python -m app.cli create-admin --email you@example.com --name "Owner"

The password is read interactively (or from stdin with --password-stdin) and never
appears in shell history. The new admin must enrol two-factor authentication at first login.
"""
import argparse
import getpass
import sys

from app.core.errors import AppError
from app.db.session import SessionLocal
from app.services import users


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return pw


def create_admin(args) -> int:
    password = _read_password(args.password_stdin)
    with SessionLocal() as db:
        try:
            user = users.create_user(
                db, actor_id=None, email=args.email, display_name=args.name, password=password, roles=["admin"],
                locale=args.locale, timezone=args.timezone, must_change_password=False,
            )
            db.commit()
        except AppError as e:
            print(f"Error: {e.code}: {e.message} {e.details or ''}", file=sys.stderr)
            return 1
    print(f"Admin user created: id={user.id} email={user.email}. Two-factor setup is required at first login.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="Create an administrator account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--locale", default="zh-TW")
    p.add_argument("--timezone", default="America/New_York")
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=create_admin)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

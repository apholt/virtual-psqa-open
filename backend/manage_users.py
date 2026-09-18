#!/usr/bin/env python3
"""
CLI utility to manage unique user accounts for HIPAA compliance (§ 164.312(a)(2)(i)).

Usage:
  python manage_users.py list
  python manage_users.py add <username> <password> [--name "Full Name"] [--role physicist]
  python manage_users.py passwd <username> <new_password>
  python manage_users.py deactivate <username>
"""
import argparse
import os
import sys
from pathlib import Path

# Auto-re-execute with project virtualenv if running outside venv
_root = Path(__file__).resolve().parent.parent
if sys.prefix == sys.base_prefix:
    for _candidate in [
        _root / ".venv_linux" / "bin" / "python",
        _root / ".venv" / "bin" / "python",
        _root / "venv" / "bin" / "python",
    ]:
        if _candidate.exists():
            os.execv(str(_candidate), [str(_candidate)] + sys.argv)

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from database import Base, SessionLocal, engine
import models
from models.user import User
from services.auth_service import create_user, hash_password, list_users

# Ensure tables exist
Base.metadata.create_all(bind=engine)


def main():
    parser = argparse.ArgumentParser(description="Manage Virtual PSQA Clinical Users (HIPAA)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # List users
    subparsers.add_parser("list", help="List all registered clinical users")

    # Add user
    add_parser = subparsers.add_parser("add", help="Add a new unique user")
    add_parser.add_argument("username", help="Unique username")
    add_parser.add_argument("password", help="User password")
    add_parser.add_argument("--name", default=None, help="Full name (e.g. 'Dr. Alice Smith')")
    add_parser.add_argument("--role", default="physicist", choices=["physicist", "dosimetrist", "admin", "auditor"])

    # Password change
    pw_parser = subparsers.add_parser("passwd", help="Change password for a user")
    pw_parser.add_argument("username", help="Target username")
    pw_parser.add_argument("new_password", help="New password")

    # Deactivate
    deact_parser = subparsers.add_parser("deactivate", help="Deactivate a user account")
    deact_parser.add_argument("username", help="Target username")

    args = parser.parse_args()
    db = SessionLocal()

    try:
        if args.command == "list":
            users = list_users(db)
            if not users:
                print("No users found in database.")
                return
            print(f"\n{'ID':<5} {'USERNAME':<18} {'ROLE':<14} {'STATUS':<10} {'NAME'}")
            print("-" * 65)
            for u in users:
                status = "ACTIVE" if u.is_active else "DISABLED"
                name = u.full_name or ""
                print(f"{u.id:<5} {u.username:<18} {u.role:<14} {status:<10} {name}")
            print()

        elif args.command == "add":
            u = create_user(
                db,
                username=args.username,
                password=args.password,
                full_name=args.name,
                role=args.role,
            )
            print(f"✓ Created user '{u.username}' ({u.role}) successfully.")

        elif args.command == "passwd":
            u = db.query(User).filter(User.username == args.username.strip().lower()).first()
            if not u:
                print(f"ERROR: User '{args.username}' not found.")
                sys.exit(1)
            u.hashed_password = hash_password(args.new_password)
            db.commit()
            print(f"✓ Password updated for user '{u.username}'.")

        elif args.command == "deactivate":
            u = db.query(User).filter(User.username == args.username.strip().lower()).first()
            if not u:
                print(f"ERROR: User '{args.username}' not found.")
                sys.exit(1)
            u.is_active = False
            db.commit()
            print(f"✓ User '{u.username}' deactivated.")

    finally:
        db.close()


if __name__ == "__main__":
    main()

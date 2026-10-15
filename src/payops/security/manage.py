"""Account and audit administration from the command line (for the person who runs the system).

    python -m payops.security.manage create-user alice --role admin     (asks for the password twice)
    python -m payops.security.manage list-users
    python -m payops.security.manage set-role bob lead
    python -m payops.security.manage disable bob        |  enable bob
    python -m payops.security.manage reset-password bob
    python -m payops.security.manage unlock bob
    python -m payops.security.manage audit [--limit 30]
    python -m payops.security.manage verify-audit

This tool connects with the application login from .env, so whoever can run it can
already read those secrets: it is an operator tool, like psql. Every change it makes is
written to the audit log as "cli:<your Windows user name>".
"""

from __future__ import annotations

import argparse
import getpass
import sys

from payops.config import get_settings
from payops.db.connection import get_history_engine
from payops.security import service
from payops.security.audit import AuditEvent, verify_chain
from payops.security.passwords import PasswordPolicyError
from payops.security.rbac import ROLES
from payops.security.service import AccountError
from payops.security.store import AuthStore, PostgresAuthStore


def cli_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"[:80]
    except Exception:  # noqa: BLE001 - no user name available
        return "cli:unknown"


def record(store: AuthStore, action: str, target: str | None = None, outcome: str = "success", **details) -> None:
    store.append_audit(AuditEvent(actor=cli_actor(), action=action, target=target, outcome=outcome,  # type: ignore[arg-type]
                                  source="cli", details=details))


def _ask_password(username: str) -> str:
    first = getpass.getpass(f"New password for {username}: ")
    if first != getpass.getpass("Repeat it: "):
        raise AccountError("the two passwords differ")
    return first


def main(argv: list[str] | None = None, store: AuthStore | None = None, ask=_ask_password) -> int:
    parser = argparse.ArgumentParser(description="Manage accounts and the audit log.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--role", required=True, choices=ROLES)
    sub.add_parser("list-users")
    r = sub.add_parser("set-role")
    r.add_argument("username")
    r.add_argument("role", choices=ROLES)
    for name in ("disable", "enable", "reset-password", "unlock"):
        sub.add_parser(name).add_argument("username")
    a = sub.add_parser("audit")
    a.add_argument("--limit", type=int, default=30)
    sub.add_parser("verify-audit")
    args = parser.parse_args(argv)

    settings = get_settings()
    if store is None:
        if not settings.history_enabled:
            print("APP_DB_PASSWORD is not set in .env. Set it and run: python -m payops.db.setup_app")
            return 1
        store = PostgresAuthStore(get_history_engine(settings))

    try:
        if args.cmd == "create-user":
            service.create_user(store, settings, args.username, args.role, ask(args.username))
            record(store, "user.create", args.username.lower(), role=args.role)
            print(f"Created {args.username.lower()} ({args.role}).")
        elif args.cmd == "list-users":
            users = store.list_users()
            print(f"{'username':<20} {'role':<8} {'active':<7} {'locked until (UTC)':<20} password changed")
            for u in users:
                locked = f"{u.locked_until:%Y-%m-%d %H:%M}" if u.locked_until else "-"
                print(f"{u.username:<20} {u.role:<8} {str(u.active):<7} {locked:<20} {u.password_changed_at:%Y-%m-%d}")
            if not users:
                print("(no users yet: create an admin with create-user <name> --role admin)")
        elif args.cmd == "set-role":
            service.change_role_or_status(store, args.username, args.role, None)
            record(store, "user.update", args.username, role=args.role)
            print(f"{args.username} is now {args.role}; their sessions were ended.")
        elif args.cmd in ("disable", "enable"):
            service.change_role_or_status(store, args.username, None, args.cmd == "enable")
            record(store, "user.update", args.username, active=args.cmd == "enable")
            print(f"{args.username} {args.cmd}d.")
        elif args.cmd == "reset-password":
            service.set_password(store, settings, args.username, ask(args.username))
            record(store, "user.password_reset", args.username, sessions_ended=True)
            print(f"Password for {args.username} reset; their sessions were ended.")
        elif args.cmd == "unlock":
            if store.get_user(args.username) is None:
                raise AccountError(f"no user '{args.username}'")
            store.record_login_success(args.username)
            record(store, "user.unlock", args.username)
            print(f"{args.username} unlocked.")
        elif args.cmd == "audit":
            for e in reversed(store.list_audit(limit=max(1, min(args.limit, 500)))):
                print(f"{e.id:>6} {e.at:%Y-%m-%d %H:%M:%S} {e.outcome:<8} {e.actor:<22} {e.action:<24} "
                      f"{e.target or '':<38} {e.details or ''}")
        elif args.cmd == "verify-audit":
            report = verify_chain(store.iter_audit())
            record(store, "audit.verify", outcome="success" if report.ok else "failure", rows=report.rows, ok=report.ok)
            if report.ok:
                print(f"Audit log intact: {report.rows} rows. Latest hash: {report.last_hash}")
                print("Keep that hash somewhere else (e.g. your notes) to detect later removal of recent rows.")
            else:
                print(f"AUDIT LOG BROKEN at row {report.first_bad_id}: {report.problem}")
                return 2
    except (AccountError, PasswordPolicyError) as exc:
        print(f"Refused: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

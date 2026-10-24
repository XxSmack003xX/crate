import argparse
import getpass
import os
import sys


def main():
    p = argparse.ArgumentParser(prog="crate", description="Artist library that auto-downloads discographies via slskd")
    p.add_argument("--host", default=os.environ.get("CRATE_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CRATE_PORT", "9325")))
    p.add_argument("--db", default=os.environ.get("CRATE_DB", "./data/crate.db"))
    sub = p.add_subparsers(dest="cmd")
    rp = sub.add_parser("reset-password", help="set a new password for an account (e.g. a locked-out admin)")
    rp.add_argument("username")
    rp.add_argument("password", nargs="?")
    ca = sub.add_parser("create-admin", help="create an admin account")
    ca.add_argument("username")
    ca.add_argument("password", nargs="?")
    sub.add_parser("list-users", help="list accounts")
    a = p.parse_args()

    if a.cmd:
        from .auth import Auth, AuthError
        from .db import DB
        auth = Auth(DB(a.db))
        try:
            if a.cmd == "list-users":
                for u in auth.list():
                    print(f"{u['username']:24} {u['role']:6} last sign-in: {u['last_login'] or 'never'}")
                return 0
            pw = a.password or getpass.getpass("New password: ")
            if a.cmd == "reset-password":
                u = auth.get(a.username)
                if not u:
                    print(f"No account named {a.username!r}", file=sys.stderr)
                    return 1
                auth.set_password(u["id"], pw)
                auth.db.x("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
                print(f"Password updated for {u['username']}.")
            else:
                auth.create(a.username, pw, role="admin", created_by="command line")
                print(f"Admin account {a.username} created.")
            return 0
        except AuthError as e:
            print(str(e), file=sys.stderr)
            return 1

    from .server import serve
    serve(a.db, a.host, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

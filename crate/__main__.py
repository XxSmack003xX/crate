import argparse
import os

from .server import serve


def main():
    p = argparse.ArgumentParser(prog="crate", description="Artist library that auto-downloads discographies via slskd")
    p.add_argument("--host", default=os.environ.get("CRATE_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CRATE_PORT", "9325")))
    p.add_argument("--db", default=os.environ.get("CRATE_DB", "./data/crate.db"))
    a = p.parse_args()
    serve(a.db, a.host, a.port)


if __name__ == "__main__":
    main()

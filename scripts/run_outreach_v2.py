#!/usr/bin/env python3
"""Explicit V2 initialization, one-shot worker and web entry point."""
import argparse
import os

from outreach_v2.db import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "serve", "tick"])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8020")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    db = Database()
    if args.command == "init":
        db.initialize()
    elif args.command == "tick":
        from outreach_v2.worker import tick

        result = tick(db)
        print(result)
        if result["status"] != "COMPLETED":
            raise SystemExit(1)
    else:
        import uvicorn

        from outreach_v2.app import create_app

        uvicorn.run(create_app(db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

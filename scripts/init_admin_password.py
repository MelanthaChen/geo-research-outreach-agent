"""Generate an Argon2id administrator password hash without echoing the password.

The hash is written to a local mode-0600 file by default.  Set the resulting
OUTREACH_ADMIN_PASSWORD_HASH value in the hosting secret manager; never put a
password or hash in source control.
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from outreach_agent.admin_auth import hash_password


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="mode-0600 file outside the repository")
    args = parser.parse_args()
    first = getpass.getpass("New administrator password (14+ characters): ")
    second = getpass.getpass("Repeat administrator password: ")
    if first != second:
        raise SystemExit("Passwords do not match")
    value = hash_password(first) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(value, encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(f"Wrote Argon2id hash to {args.output} (mode 0600). Set only OUTREACH_ADMIN_PASSWORD_HASH from this file in the secret manager.")


if __name__ == "__main__":
    main()

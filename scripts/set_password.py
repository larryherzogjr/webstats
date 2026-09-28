#!/usr/bin/env python3
"""Generate a bcrypt password hash and update config.toml safely."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import tempfile
from typing import Optional

import bcrypt


PASSWORD_LINE = re.compile(r'^(\s*admin_password_hash\s*=\s*)"[^"]*"(\s*(?:#.*)?)$', re.MULTILINE)


def set_password(config_path: Path, password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    source = config_path.read_text(encoding="utf-8")
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    replacement = lambda match: f'{match.group(1)}"{password_hash}"{match.group(2)}'
    updated, count = PASSWORD_LINE.subn(replacement, source, count=1)
    if count != 1:
        raise ValueError("Could not find one admin_password_hash setting")
    metadata = config_path.stat()
    mode = metadata.st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent, prefix=config_path.name + ".", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, config_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/webstats/config.toml")
    args = parser.parse_args(argv)
    first = getpass.getpass("New admin password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        parser.error("Passwords do not match")
    try:
        set_password(Path(args.config), first)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Updated password hash in {args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

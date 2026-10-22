#!/usr/bin/env python3
"""Install or explicitly update a complete Faygo CLI release."""
import argparse
import os
import sys

from faygo_client.deployment import install

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="обновить существующий клиент и движок")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        install(force=args.force)
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Ошибка установки: {error}", file=sys.stderr)
        sys.exit(1)

"""``python -m controlplane [serve|migrate]``: run the API (default) or apply migrations."""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="controlplane")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "migrate"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "migrate":
        from .migrate import upgrade
        from .settings import get_settings

        upgrade(get_settings().database_url)
        logging.getLogger("controlplane").info("database is at the latest revision")
        return
    uvicorn.run(
        "controlplane.app:create_app",
        factory=True,
        host=os.environ.get("CP_HOST", "0.0.0.0"),
        port=int(os.environ.get("CP_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("CP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=os.environ.get("CP_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()

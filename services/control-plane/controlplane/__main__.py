"""``python -m controlplane``: run the API with uvicorn."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

"""Entry point: ``lampmonitor`` or ``python -m lampmonitor``."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import AppConfig


def main() -> None:
    config = AppConfig.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every snapshot request at INFO, which drowns everything else.
    logging.getLogger("httpx").setLevel(max(logging.WARNING, logging.getLogger().level))
    if not config.password:
        logging.getLogger("lampmonitor").warning(
            "LAMPMONITOR_PASSWORD is not set: the web UI is open to anyone who can reach it"
        )
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()

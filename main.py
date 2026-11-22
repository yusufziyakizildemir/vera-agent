"""VERA 7/24 Ana Başlatıcı."""

from __future__ import annotations

import logging

import uvicorn

from vera.api import create_app
from vera.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)

settings = get_settings()
app = create_app(settings=settings, start_background=True)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )

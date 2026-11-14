from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.core.config import data_dir

LOGGER_NAME = "PhoneNAS"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_dir():
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

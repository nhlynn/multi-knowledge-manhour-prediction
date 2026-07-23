"""Logging configuration for MHES."""

import os
import logging
from logging.handlers import RotatingFileHandler


def setup_logging(log_folder: str, log_level: int = logging.INFO) -> None:
    """Configure application-wide logging with file and console handlers.

    Args:
        log_folder: Path to the log files directory.
        log_level: Logging level (default: INFO).
    """
    os.makedirs(log_folder, exist_ok=True)

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(log_format)

    # File handler with rotation
    file_handler = RotatingFileHandler(
        os.path.join(log_folder, "mhes.log"),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

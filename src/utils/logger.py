import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(
    name: str,
    level: str = "INFO",
    log_dir: Path | None = None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level.upper())
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "forex-ea.log",
            maxBytes=5_000_000,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger

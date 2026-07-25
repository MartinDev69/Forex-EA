import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that would otherwise inherit root's INFO level and drown
# the trading log in HTTP/scheduler chatter. Absent packages are harmless --
# getLogger just creates a placeholder.
_NOISY = ("urllib3", "requests", "httpx", "httpcore", "asyncio", "apscheduler", "telegram")


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
) -> logging.Logger:
    """Configure the ROOT logger and return the app's own logger.

    Handlers go on root, not on a named logger, so that every module using the
    conventional `logging.getLogger(__name__)` -- i.e. all of src.* -- inherits
    them. Attaching them to a logger named "forex-ea" instead (as this function
    used to) meant only main.py's own records ever reached forex-ea.log: the
    src.* tree has no handlers of its own, so its INFO was dropped at root's
    default WARNING level and its warnings fell through to logging.lastResort,
    a bare unformatted stderr handler.
    """
    root = logging.getLogger()
    if root.handlers:  # idempotent -- safe to call twice
        return logging.getLogger("forex-ea")

    root.setLevel(level.upper())
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "forex-ea.log",
            maxBytes=5_000_000,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    # No handlers and no propagate=False here: this logger must reach root's
    # handlers like every other one, or main.py's lines would double-write.
    return logging.getLogger("forex-ea")


def get_logger(
    name: str,
    level: str = "INFO",
    log_dir: Path | None = None,
) -> logging.Logger:
    """Back-compat shim for the old per-name setup. Prefer setup_logging()."""
    setup_logging(level=level, log_dir=log_dir)
    return logging.getLogger(name)

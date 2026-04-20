import logging
import os

import structlog

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(pad_event=False),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging._nameToLevel.get(LOG_LEVEL, logging.INFO)
    ),
)

# OpenAI SDK uses httpx; it logs every request at INFO by default.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

try:
    from importlib.metadata import version
    __version__ = version("bigdata_briefs")
except Exception:  # Package not installed (e.g. run from notebook via sys.path)
    __version__ = "0.0.0.dev"
logger = structlog.get_logger()

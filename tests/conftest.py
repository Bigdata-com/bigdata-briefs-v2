import os
import sys
from pathlib import Path

# Prefer this package source tree when tests are collected from a parent workspace.
_pkg_root = Path(__file__).resolve().parents[1]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

TEST_DATABASE_DB_STRING = "sqlite:////temp/briefs/test.db"

# Override environment variables for testing
os.environ.update(
    {
        "BIGDATA_API_KEY": "fake-key",
        "OPENAI_API_KEY": "fake-key",
        "DB_STRING": TEST_DATABASE_DB_STRING,
        "LOG_LEVEL": "ERROR",
    }
)

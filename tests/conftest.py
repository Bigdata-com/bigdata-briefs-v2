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

import pytest

from bigdata_briefs import key_health


@pytest.fixture(autouse=True)
def _no_outbound_key_probes(monkeypatch):
    """Keep key_health from making real network calls during tests.

    conftest sets BIGDATA_API_KEY/OPENAI_API_KEY to "fake-key", so an
    unpatched preflight would actually call OpenAI and Bigdata, get a
    401/403 back and turn every route that guards on it into a 503.
    Tests that exercise the checks themselves patch these explicitly.
    """
    monkeypatch.setattr(key_health, "check_all_keys", lambda force=False: [])
    monkeypatch.setattr(key_health, "log_key_health", list)

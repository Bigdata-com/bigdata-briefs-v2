"""Guard tests for BRIEFS_MODE router mounting.

Locks the rule that the default ('both') mounts every stateful route that exists
today AND the stateless routes, and that each single mode mounts only its own surface.
"""

from __future__ import annotations

import pytest

from bigdata_briefs.api.app import create_app
from bigdata_briefs.settings import settings

_STATELESS = {
    "/api/v1/stateless/briefs",
    "/api/v1/stateless/jobs/{job_id}",
}
_SAMPLE_STATEFUL = {"/api/v1/batch/run-parallel", "/api/v1/reports/bullets"}


def _paths(mode, monkeypatch):
    monkeypatch.setattr(settings, "BRIEFS_MODE", mode)
    return {r.path for r in create_app().routes}


def test_both_mounts_stateful_and_stateless(monkeypatch):
    paths = _paths("both", monkeypatch)
    assert _STATELESS <= paths
    assert _SAMPLE_STATEFUL <= paths


def test_stateless_only_omits_stateful(monkeypatch):
    paths = _paths("stateless", monkeypatch)
    assert _STATELESS <= paths
    assert not (_SAMPLE_STATEFUL & paths)


def test_stateful_only_omits_stateless(monkeypatch):
    paths = _paths("stateful", monkeypatch)
    assert _SAMPLE_STATEFUL <= paths
    assert not (_STATELESS & paths)

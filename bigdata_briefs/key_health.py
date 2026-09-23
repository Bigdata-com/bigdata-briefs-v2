"""Validation of the outbound API keys the pipeline depends on.

Checks whether the keys used to call external services (OpenAI and
Bigdata.com) are actually *accepted* by those services, so a revoked or
mistyped key surfaces as a clear log line at startup (and via
``GET /health/keys``) instead of as an opaque failure in the middle of a
long run.

A check distinguishes three outcomes:
  - ``ok=True``  — upstream accepted the key.
  - ``ok=False`` — upstream rejected it (401/403): the key is wrong/revoked.
  - ``ok=None``  — couldn't tell: key not set, or upstream unreachable
                   (network/timeout). We don't cry "bad key" on a blip.

Inbound auth (``PIPELINE_API_KEY``) is intentionally not checked here: it
is *our* key, there is no upstream to validate it against.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import httpx
import openai

from bigdata_briefs import logger
from bigdata_briefs.exceptions import InvalidAPIKeyError
from bigdata_briefs.settings import UNSET, settings

# On-demand checks are cached so the /health/keys endpoint can be polled
# without hammering upstream (or burning OpenAI rate limit) on every hit.
# Startup populates the cache; subsequent reads serve it until stale.
_CACHE_TTL_SECONDS = 60.0
# A probe must fail fast — it must not hang on a slow upstream the way a
# real pipeline call (API_TIMEOUT_SECONDS=120, LLM_TIMEOUT_SECONDS=60) can.
_PROBE_TIMEOUT_SECONDS = 10.0

_cache: dict | None = None  # {"at": monotonic_ts, "statuses": list[KeyStatus]}


@dataclass
class KeyStatus:
    name: str
    configured: bool
    # True = accepted, False = rejected (bad key), None = undetermined.
    ok: bool | None
    detail: str


def check_openai_key() -> KeyStatus:
    """Validate ``OPENAI_API_KEY`` with a no-token ``models.list()`` call."""
    name = "OPENAI_API_KEY"
    if settings.OPENAI_API_KEY == UNSET or not settings.OPENAI_API_KEY:
        return KeyStatus(name, configured=False, ok=None, detail="not set")
    client = openai.OpenAI(
        api_key=str(settings.OPENAI_API_KEY),
        timeout=_PROBE_TIMEOUT_SECONDS,
        max_retries=0,
    )
    try:
        client.models.list()  # authenticated, cheap, consumes no tokens
        return KeyStatus(name, configured=True, ok=True, detail="valid")
    except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
        return KeyStatus(
            name, configured=True, ok=False, detail=f"rejected ({exc.status_code})"
        )
    except (openai.APIConnectionError, openai.APITimeoutError) as exc:
        return KeyStatus(
            name, configured=True, ok=None,
            detail=f"upstream unreachable: {type(exc).__name__}",
        )
    except openai.APIStatusError as exc:
        # Non-auth status (e.g. 429/5xx): the key itself was accepted.
        return KeyStatus(
            name, configured=True, ok=True,
            detail=f"valid (upstream returned {exc.status_code})",
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never crash startup
        return KeyStatus(
            name, configured=True, ok=None, detail=f"check failed: {type(exc).__name__}"
        )


def check_bigdata_key() -> KeyStatus:
    """Validate ``BIGDATA_API_KEY`` against an authenticated KG endpoint.

    Only the *auth* outcome matters: any non-401/403 response means the key
    was accepted (even a 4xx on the empty payload). Network errors are
    reported as undetermined, not as a bad key.
    """
    name = "BIGDATA_API_KEY"
    if settings.BIGDATA_API_KEY == UNSET or not settings.BIGDATA_API_KEY:
        return KeyStatus(name, configured=False, ok=None, detail="not set")
    try:
        resp = httpx.request(
            "POST",
            f"{settings.API_BASE_URL}/v1/knowledge-graph/entities/id",
            headers={
                "X-API-KEY": str(settings.BIGDATA_API_KEY),
                "Content-Type": "application/json",
            },
            json={"values": []},
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return KeyStatus(
            name, configured=True, ok=None,
            detail=f"upstream unreachable: {type(exc).__name__}",
        )
    if resp.status_code in (401, 403):
        return KeyStatus(
            name, configured=True, ok=False, detail=f"rejected ({resp.status_code})"
        )
    return KeyStatus(
        name, configured=True, ok=True, detail=f"valid (status {resp.status_code})"
    )


def check_all_keys(force: bool = False) -> list[KeyStatus]:
    """Run every key check, served from a TTL cache unless ``force`` is set."""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["statuses"]
    statuses = [check_openai_key(), check_bigdata_key()]
    _cache = {"at": now, "statuses": statuses}
    return statuses


def log_key_health() -> list[KeyStatus]:
    """Run the checks and emit one log line per key. Never raises."""
    statuses = check_all_keys(force=True)
    for s in statuses:
        if s.ok is True:
            logger.info(f"API key check: {s.name} OK ({s.detail})")
        elif s.ok is False:
            logger.error(
                f"API key check: {s.name} INVALID — {s.detail}. "
                "Calls using this key will fail; fix it in .env / secrets."
            )
        else:
            logger.warning(
                f"API key check: {s.name} could not be verified — {s.detail}."
            )
    return statuses


def rejected_keys() -> list[KeyStatus]:
    """Keys upstream actively *rejected* (ok is False), served from cache.

    Excludes ``ok=None`` (unset / unreachable): we abort a run only on a key
    that is provably wrong, never on a transient network blip.
    """
    return [s for s in check_all_keys() if s.ok is False]


def preflight_keys() -> None:
    """Raise ``InvalidAPIKeyError`` if any key was rejected. Call before a run.

    Cheap to call repeatedly: results come from the TTL cache, so a large
    batch probes upstream at most once per ``_CACHE_TTL_SECONDS``.
    """
    rejected = rejected_keys()
    if rejected:
        names = "; ".join(f"{s.name} {s.detail}" for s in rejected)
        raise InvalidAPIKeyError(
            f"{names}. Calls using this key would fail; fix it in .env / secrets."
        )


def health_payload(statuses: list[KeyStatus]) -> dict:
    """Build the JSON body for the /health/keys endpoint. Never echoes keys."""
    invalid = [s for s in statuses if s.ok is False]
    return {"ok": not invalid, "keys": [asdict(s) for s in statuses]}

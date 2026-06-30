class TooManyAPIRetriesError(Exception):
    """TooManyAPIRetriesError"""


class InvalidAPIKeyError(Exception):
    """An outbound API key (OpenAI/Bigdata) was rejected by its upstream.

    Raised by ``key_health.preflight_keys()`` to abort a run before any
    expensive work, instead of failing deep into the pipeline with retries.
    """

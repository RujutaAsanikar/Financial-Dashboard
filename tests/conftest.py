"""Session-wide guarantee that no test reaches the network.

Until now this held by accident: nothing loaded .env, so a test process had no
ANTHROPIC_API_KEY and any stray model call failed at the client. query.py now
loads .env on import (so `uvicorn main:app` has a key without the caller
exporting one), which imports the key into the pytest process too -- and a
stray call would spend real money against a real key instead of failing.

So make it structural. Keys are stripped for the whole session, and the two
client factories are replaced with ones that fail the test loudly. Individual
test files still mock at their own level; this is the backstop underneath.
"""
import os

import pytest

_API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "TRIQ_API_KEY",
)


@pytest.fixture(autouse=True, scope="session")
def _no_api_keys():
    """Remove every credential for the duration of the run."""
    saved = {name: os.environ.pop(name, None) for name in _API_KEY_VARS}
    yield
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _no_live_model_calls(monkeypatch):
    """Fail any test that builds a real model client.

    Patched by name on the module, not via setattr on the function object:
    `monkeypatch.setattr(mod._client, "__call__", ...)` does nothing, because
    calling `mod._client()` resolves __call__ on `function`, not on that one
    function instance.
    """
    import query

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "A test tried to construct a live model client. Mock the function "
            "that calls it (query._generate_sql, query._summarize, ...) rather "
            "than letting the request out."
        )

    monkeypatch.setattr(query, "_client", forbidden)

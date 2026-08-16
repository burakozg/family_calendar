"""Unhandled exceptions must reach the activity log. Before this, an app bug
surfaced only as a generic frontend toast plus a traceback in `docker logs`:
one malformed recipe 500'd every meal-planner call for days with no log line."""
import asyncio
import json

import activity_log
import main
import pytest
from starlette.requests import Request


@pytest.fixture()
def boom_route():
    """Register a route that raises, and take it back out afterwards. It goes to
    the front of the table because main.py mounts the frontend at "/" — anything
    appended after that catch-all is unreachable."""
    def boom():
        raise RuntimeError("kaboom")
    main.app.add_api_route("/__boom", boom, methods=["GET"])
    main.app.router.routes.insert(0, main.app.router.routes.pop())
    yield "/__boom"
    main.app.router.routes = [r for r in main.app.router.routes
                              if getattr(r, "path", "") != "/__boom"]


def test_unhandled_exception_is_logged(client, boom_route):
    # Starlette runs the handler and *then* re-raises, so the TestClient's default
    # raise_server_exceptions surfaces the original error — the log write below is
    # what proves the handler ran on the way past.
    with pytest.raises(RuntimeError):
        client.get(boom_route)

    entry = activity_log.read_logs(category="system", q="kaboom")[0]
    assert entry["level"] == "error"
    assert entry["action"] == "unhandled"
    assert "GET /__boom" in entry["message"]
    assert entry["detail"]["type"] == "RuntimeError"
    assert "RuntimeError: kaboom" in entry["detail"]["traceback"]


def test_response_body_leaks_nothing(client):
    """The client gets a bare 500 — the type, message, and traceback go to the
    log only. Called directly because the TestClient re-raises before a caller
    could inspect the response."""
    scope = {"type": "http", "method": "POST", "path": "/secret", "http_version": "1.1",
             "scheme": "http", "server": ("testserver", 80), "query_string": b"",
             "root_path": "", "headers": []}
    resp = asyncio.run(main._log_unhandled(Request(scope), RuntimeError("db password hunter2")))

    assert resp.status_code == 500
    assert json.loads(resp.body) == {"detail": "Internal Server Error"}
    assert activity_log.read_logs(category="system", q="hunter2")   # …but it is logged


def test_httpexception_is_untouched(client):
    """Deliberate HTTPExceptions keep their own status and detail, and must not
    be logged as unhandled — only genuine bugs are."""
    before = len(activity_log.read_logs(category="system", q="unhandled", limit=1000))
    resp = client.post("/meals/plan/generate", json={"weekSummary": ""})

    assert resp.status_code == 500                      # ProviderNotConfigured
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]  # the real cause, surfaced
    assert len(activity_log.read_logs(category="system", q="unhandled", limit=1000)) == before

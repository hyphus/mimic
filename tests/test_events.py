"""Phase 3b: the event broker and the SSE endpoint that rides on it."""

from __future__ import annotations

import json
import logging

from pathlib import Path

import pytest

from mimic import events
from mimic.httplog import setup as logsetup, store as logstore
from tests.helpers import csrf_token

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_leaked_subscribers():
    yield
    assert events.subscriber_count() == 0


def _login(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})


def test_publish_reaches_every_subscriber():
    a, b = events.subscribe(), events.subscribe()
    try:
        events.publish("httplog", {"id": 1})
        assert a.get_nowait() == ("httplog", {"id": 1})
        assert b.get_nowait() == ("httplog", {"id": 1})
    finally:
        events.unsubscribe(a)
        events.unsubscribe(b)


def test_publish_never_blocks_and_overflow_drops_to_a_resync():
    q = events.subscribe()
    try:
        for i in range(events.QUEUE_MAXSIZE + 5):
            events.publish("httplog", {"id": i})
        # The backlog is discarded, not grown: the client is told to re-fetch and
        # then carries on with whatever arrived after the marker.
        assert q.qsize() <= events.QUEUE_MAXSIZE
        assert q.get_nowait() == ("resync", {})
        # The event that hit the full queue is dropped along with the backlog; the
        # resync fetch is what recovers it.
        assert q.get_nowait() == ("httplog", {"id": events.QUEUE_MAXSIZE + 1})
    finally:
        events.unsubscribe(q)


def test_frame_is_well_formed_sse():
    assert events.frame("applog", {"text": "hi"}) == b'event: applog\ndata: {"text": "hi"}\n\n'


def test_stream_yields_backlog_then_unsubscribes_on_close():
    gen = events.stream(iter([("applog", {"id": 1, "text": "old"})]))
    assert next(gen) == b": connected\n\n"
    assert events.subscriber_count() == 1
    assert next(gen) == events.frame("applog", {"id": 1, "text": "old"})
    gen.close()
    assert events.subscriber_count() == 0


def test_insert_log_publishes_the_rendered_row(app):
    q = events.subscribe()
    try:
        rid = logstore.insert_log(
            method="GET",
            path="/httpbin/get",
            query_string=None,
            module_id="httpbin",
            request_headers={},
            request_body=None,
            response_status=200,
            response_headers={},
            response_body="{}",
        )
        event, data = q.get_nowait()
        assert event == "httplog"
        assert data["id"] == rid
        assert data["path"] == "/httpbin/get"
        assert data["ts_display"].endswith("UTC")
    finally:
        events.unsubscribe(q)


def test_applog_keeps_publishing_past_the_buffer_cap(app):
    """The old implementation stopped updating once the buffer filled."""
    handler = logsetup.LogOutputHandler()
    handler.setFormatter(logging.Formatter("{message}", style="{"))
    logger = logging.getLogger("mimic.tests.applog")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    q = events.subscribe()
    try:
        overflow = logsetup.DASHBOARD_BUFFER_SIZE + 10
        for i in range(overflow):
            logger.info(f"message {i}")
        received = [q.get_nowait() for _ in range(overflow)]
    finally:
        events.unsubscribe(q)
        logger.removeHandler(handler)

    assert all(event == "applog" for event, _ in received)
    assert received[-1][1]["text"].endswith(f"message {overflow - 1}")
    # Ids stay monotonic so a resyncing client knows where it left off.
    ids = [data["id"] for _, data in received]
    assert ids == sorted(ids)
    assert len(logsetup.get_dashboard_log_entries()) == logsetup.DASHBOARD_BUFFER_SIZE


def test_events_endpoint_requires_login(client):
    resp = client.get("/admin/events")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_events_endpoint_streams(client):
    _login(client)
    resp = client.get("/admin/events", buffered=False)
    try:
        assert resp.mimetype == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-cache"
        assert next(resp.response) == b": connected\n\n"
        assert events.subscriber_count() == 1
    finally:
        resp.close()


def test_events_endpoint_replays_the_dashboard_backlog(client, app):
    _login(client)
    logging.getLogger("mimic.tests.backlog").warning("earlier message")
    resp = client.get("/admin/events", buffered=False)
    try:
        frames = resp.response
        assert next(frames) == b": connected\n\n"
        replayed = [
            json.loads(next(frames).split(b"data: ", 1)[1])
            for _ in logsetup.get_dashboard_log_entries()
        ]
    finally:
        resp.close()
    assert any("earlier message" in entry["text"] for entry in replayed)


def test_request_log_page_uses_sse_not_a_polling_timer():
    source = (REPO_ROOT / "mimic/admin/static/js/main.js").read_text()
    assert "new EventSource(eventsUrl)" in source
    assert 'addEventListener("httplog"' in source
    assert "setInterval" not in source

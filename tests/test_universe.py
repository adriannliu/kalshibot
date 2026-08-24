from __future__ import annotations

import pytest

from config import exchange
from feed import universe
from feed.rest_client import RestClient, RestError


class RecordingClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on

    def paginate(self, endpoint, collection, params=None, page_limit=1000, max_pages=200):
        self.calls.append((endpoint, page_limit))
        if self._fail_on and endpoint == self._fail_on:
            raise RestError(400, endpoint, '{"error":"bad_request"}')
        return iter([{"event_ticker": "E-1"}])

    events = RestClient.events
    markets = RestClient.markets


def test_events_uses_the_smaller_page_limit_the_endpoint_allows():
    client = RecordingClient()
    list(client.events(series_ticker="KXHIGHNY"))
    endpoint, page_limit = client.calls[-1]
    assert endpoint == exchange.ENDPOINT_EVENTS
    assert page_limit == exchange.EVENTS_PAGE_LIMIT == 200


def test_markets_keeps_the_larger_page_limit():
    client = RecordingClient()
    list(client.markets(status="open"))
    endpoint, page_limit = client.calls[-1]
    assert endpoint == exchange.ENDPOINT_MARKETS
    assert page_limit == exchange.MARKETS_PAGE_LIMIT == 1000


def test_event_index_failure_is_recorded_not_swallowed():
    universe.EVENT_INDEX_FAILURES.clear()
    client = RecordingClient(fail_on=exchange.ENDPOINT_EVENTS)

    index = universe.fetch_event_index(client, "KXHIGHNY")

    assert index == {}
    assert len(universe.EVENT_INDEX_FAILURES) == 1
    assert "KXHIGHNY" in universe.EVENT_INDEX_FAILURES[0]


def test_event_index_returns_records_when_the_call_succeeds():
    universe.EVENT_INDEX_FAILURES.clear()
    index = universe.fetch_event_index(RecordingClient(), "KXHIGHNY")
    assert index == {"E-1": {"event_ticker": "E-1"}}
    assert universe.EVENT_INDEX_FAILURES == []

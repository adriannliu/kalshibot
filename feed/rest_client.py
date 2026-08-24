from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from auth.credentials import Credentials
from auth.signer import RequestSigner
from config import exchange

USER_AGENT = "kalshi-mm-phase1/0.1"


class RestError(RuntimeError):
    def __init__(self, status: int, endpoint: str, body: str) -> None:
        super().__init__("%s %s -> %s" % (status, endpoint, body[:400]))
        self.status = status
        self.endpoint = endpoint
        self.body = body


class RestClient:
    def __init__(
        self,
        credentials: Credentials,
        signer: RequestSigner,
        timeout: float = 15.0,
        max_retries: int = 4,
    ) -> None:
        self._base = credentials.rest_base
        self._signer = signer
        self._timeout = timeout
        self._max_retries = max_retries

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = ""
        if params:
            cleaned = {k: v for k, v in params.items() if v is not None}
            if cleaned:
                query = "?" + urllib.parse.urlencode(cleaned)
        url = self._base + endpoint + query

        attempt = 0
        while True:
            attempt += 1
            headers = self._signer.rest_headers("GET", endpoint)
            headers["Accept"] = "application/json"
            headers["User-Agent"] = USER_AGENT
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                retryable = error.code == 429 or error.code >= 500
                if not retryable or attempt > self._max_retries:
                    raise RestError(error.code, endpoint, body)
                time.sleep(min(2 ** attempt * 0.5, 8.0))
            except urllib.error.URLError:
                if attempt > self._max_retries:
                    raise
                time.sleep(min(2 ** attempt * 0.5, 8.0))

    def paginate(
        self,
        endpoint: str,
        collection: str,
        params: Optional[Dict[str, Any]] = None,
        page_limit: int = 1000,
        max_pages: int = 200,
    ) -> Iterator[Dict[str, Any]]:
        cursor: Optional[str] = None
        pages = 0
        while pages < max_pages:
            pages += 1
            page_params = dict(params or {})
            page_params["limit"] = page_limit
            if cursor:
                page_params["cursor"] = cursor
            payload = self.get(endpoint, page_params)
            items = payload.get(collection) or []
            for item in items:
                yield item
            cursor = payload.get("cursor") or None
            if not cursor or not items:
                return

    def account_limits(self) -> Dict[str, Any]:
        return self.get(exchange.ENDPOINT_LIMITS)

    def endpoint_costs(self) -> Dict[str, Any]:
        return self.get(exchange.ENDPOINT_ENDPOINT_COSTS)

    def fee_tiers(self) -> Dict[str, Any]:
        return self.get(exchange.ENDPOINT_FEE_TIERS)

    def markets(self, **params: Any) -> Iterator[Dict[str, Any]]:
        return self.paginate(
            exchange.ENDPOINT_MARKETS, "markets", params,
            page_limit=exchange.MARKETS_PAGE_LIMIT,
        )

    def events(self, **params: Any) -> Iterator[Dict[str, Any]]:
        return self.paginate(
            exchange.ENDPOINT_EVENTS, "events", params,
            page_limit=exchange.EVENTS_PAGE_LIMIT,
        )

    def orderbook(self, ticker: str, depth: int = 0) -> Dict[str, Any]:
        endpoint = exchange.ENDPOINT_ORDERBOOK.format(ticker=urllib.parse.quote(ticker, safe=""))
        return self.get(endpoint, {"depth": depth} if depth else None)

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from feed import recorder as rec

BOOK_SID = 1
TRADE_SID = 2


class SyntheticSession:
    def __init__(self, root: str, session_id: str = "synthetic") -> None:
        self.directory = os.path.join(root, session_id)
        os.makedirs(self.directory, exist_ok=True)
        self.session_id = session_id
        self._records: List[Dict[str, Any]] = []
        self._ordinal = 0
        self._seq = 0
        self._mono_ns = 0
        self._recv_ns = 1_750_000_000_000_000_000
        self._markets: List[Dict[str, Any]] = []

    def advance(self, seconds: float) -> "SyntheticSession":
        step = int(seconds * 1e9)
        self._mono_ns += step
        self._recv_ns += step
        return self

    @property
    def now_ns(self) -> int:
        return self._mono_ns

    def _append(self, kind: str, body: Dict[str, Any]) -> None:
        self._ordinal += 1
        record = {
            "n": self._ordinal,
            "k": kind,
            "recv_ns": self._recv_ns,
            "mono_ns": self._mono_ns,
        }
        record.update(body)
        self._records.append(record)

    def _raw(self, payload: Dict[str, Any]) -> None:
        self._append(rec.KIND_WS, {"raw": json.dumps(payload)})

    def market(
        self,
        ticker: str,
        series_ticker: Optional[str] = None,
        event_ticker: str = "",
        vertical: str = "test",
        category: str = "Test",
        fee_type: str = "quadratic",
        fee_multiplier: str = "1",
        maker_multiplier: str = "0",
        taker_multiplier: str = "1",
        fee_regime: str = "maker_free",
        strike_type: str = "",
        floor_strike: Optional[str] = None,
        cap_strike: Optional[str] = None,
        mutually_exclusive: Optional[bool] = None,
    ) -> "SyntheticSession":
        self._markets.append(
            {
                "ticker": ticker,
                "series_ticker": series_ticker or ticker.split("-", 1)[0],
                "event_ticker": event_ticker,
                "vertical": vertical,
                "category": category,
                "close_time": None,
                "fee_type": fee_type,
                "fee_multiplier": fee_multiplier,
                "maker_multiplier": maker_multiplier,
                "taker_multiplier": taker_multiplier,
                "fee_regime": fee_regime,
                "strike_type": strike_type,
                "floor_strike": floor_strike,
                "cap_strike": cap_strike,
                "mutually_exclusive": mutually_exclusive,
            }
        )
        return self

    def subscribe(self, tickers: Sequence[str]) -> "SyntheticSession":
        self._append(
            rec.KIND_SUBSCRIBED,
            {"d": {"sid": BOOK_SID, "channel": "orderbook_delta", "tickers": list(tickers)}},
        )
        self._append(rec.KIND_SUBSCRIBED, {"d": {"sid": TRADE_SID, "channel": "trade", "tickers": list(tickers)}})
        return self

    def snapshot(
        self,
        ticker: str,
        yes: Sequence[Sequence[str]],
        no: Sequence[Sequence[str]],
        seq: Optional[int] = None,
    ) -> "SyntheticSession":
        self._seq = self._seq + 1 if seq is None else seq
        self._raw(
            {
                "type": "orderbook_snapshot",
                "sid": BOOK_SID,
                "seq": self._seq,
                "msg": {
                    "market_ticker": ticker,
                    "yes_dollars_fp": [list(level) for level in yes],
                    "no_dollars_fp": [list(level) for level in no],
                    "ts_ms": self._recv_ns // 1_000_000,
                },
            }
        )
        return self

    def delta(
        self,
        ticker: str,
        side: str,
        price: str,
        amount: str,
        seq: Optional[int] = None,
    ) -> "SyntheticSession":
        self._seq = self._seq + 1 if seq is None else seq
        self._raw(
            {
                "type": "orderbook_delta",
                "sid": BOOK_SID,
                "seq": self._seq,
                "msg": {
                    "market_ticker": ticker,
                    "price_dollars": price,
                    "delta_fp": amount,
                    "side": side,
                    "ts_ms": self._recv_ns // 1_000_000,
                },
            }
        )
        return self

    def skip_seq(self, count: int = 1) -> "SyntheticSession":
        self._seq += count
        return self

    def trade(
        self,
        ticker: str,
        yes_price: str,
        count: str = "10.00",
        taker_outcome_side: str = "yes",
        taker_book_side: str = "bid",
    ) -> "SyntheticSession":
        from decimal import Decimal

        no_price = str(Decimal("1") - Decimal(yes_price))
        self._raw(
            {
                "type": "trade",
                "sid": TRADE_SID,
                "msg": {
                    "market_ticker": ticker,
                    "yes_price_dollars": yes_price,
                    "no_price_dollars": no_price,
                    "count_fp": count,
                    "taker_outcome_side": taker_outcome_side,
                    "taker_book_side": taker_book_side,
                    "ts_ms": self._recv_ns // 1_000_000,
                },
            }
        )
        return self

    def reconnect(self) -> "SyntheticSession":
        self._append(rec.KIND_CONNECTION, {"d": {"event": "reconnect_scheduled", "delay_seconds": 1}})
        self._seq = 0
        return self

    def write(self, use_yes_price: bool = False) -> str:
        manifest = {
            "session_id": self.session_id,
            "started_at_ms": self._recv_ns // 1_000_000,
            "environment": "demo",
            "site": "test",
            "shard_index": 0,
            "shard_count": 1,
            "config": {"use_yes_price": use_yes_price},
            "universe": {"count": len(self._markets), "markets": self._markets},
        }
        path = os.path.join(self.directory, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        with open(os.path.join(self.directory, "capture-0000.jsonl"), "w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record, separators=(",", ":")))
                handle.write("\n")
        return self.directory

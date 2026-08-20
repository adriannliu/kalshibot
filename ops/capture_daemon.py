from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import websockets

from auth.credentials import Credentials, load_from_env
from auth.signer import RequestSigner
from config import exchange
from feed import recorder as rec
from feed import universe as universe_module
from feed.book import BookIntegrityError, BookSet, BookState, GapEvent, GapKind
from feed.recorder import RecorderConfig, SessionRecorder, write_session_manifest
from feed.rest_client import RestClient
from feed.ws_client import ReceivedMessage, ReconnectPolicy, WebSocketTransport
from ops.monitor import CaptureMonitor

BOOK_CHANNEL = exchange.ORDERBOOK_CHANNEL
TYPE_SNAPSHOT = "orderbook_snapshot"
TYPE_DELTA = "orderbook_delta"
TYPE_SUBSCRIBED = "subscribed"
TYPE_OK = "ok"
TYPE_ERROR = "error"
TYPE_TICKER = "ticker"
TYPE_TRADE = "trade"

KNOWN_TYPES = frozenset(
    {TYPE_SNAPSHOT, TYPE_DELTA, TYPE_SUBSCRIBED, TYPE_OK, TYPE_ERROR, TYPE_TICKER, TYPE_TRADE, "unsubscribed"}
)


@dataclass
class CaptureConfig:
    markets_path: str = "config/markets.yaml"
    data_root: str = "data"
    session_id: Optional[str] = None
    shard_size: int = exchange.MARKETS_PER_SUBSCRIPTION
    use_yes_price: bool = exchange.USE_YES_PRICE
    stale_feed_seconds: float = 30.0
    digest_interval_seconds: float = 300.0
    health_interval_seconds: float = 60.0
    resync_retry_seconds: float = 5.0
    max_session_seconds: Optional[float] = None
    universe_refresh_seconds: Optional[float] = None
    universe_file: Optional[str] = None
    shard_index: int = 0
    shard_count: int = 1
    site: Optional[str] = None


def git_revision() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:
        return None


class CaptureDaemon:
    def __init__(self, config: CaptureConfig, credentials: Optional[Credentials] = None) -> None:
        self._config = config
        self._credentials = credentials or load_from_env()
        self._signer = RequestSigner(self._credentials)
        self._rest = RestClient(self._credentials, self._signer)
        self._session_id = config.session_id or time.strftime(
            "%Y%m%dT%H%M%SZ", time.gmtime()
        ) + "-" + uuid.uuid4().hex[:8]
        self._recorder = SessionRecorder(
            RecorderConfig(root=config.data_root, session_id=self._session_id)
        )
        self._monitor = CaptureMonitor(started_mono=time.monotonic())
        self._books = BookSet()
        self._transport = WebSocketTransport(self._credentials, self._signer)
        self._policy = ReconnectPolicy()
        self._resolution: Optional[universe_module.UniverseResolution] = None
        self._shard_markets: List[universe_module.SelectedMarket] = []
        self._tickers: List[str] = []
        self._pending_subscriptions: Dict[int, Dict[str, Any]] = {}
        self._pending_resync: Dict[int, float] = {}
        self._last_digest_mono = 0.0
        self._last_health_mono = 0.0
        self._stop = asyncio.Event()
        self._started_mono = time.monotonic()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def directory(self) -> str:
        return self._recorder.directory

    def request_stop(self) -> None:
        self._stop.set()

    def _resolve_universe(self) -> universe_module.UniverseResolution:
        if self._config.universe_file:
            return universe_module.load_resolution(self._config.universe_file)
        spec = universe_module.load_spec(self._config.markets_path)
        return universe_module.resolve(self._rest, spec)

    def _shards(self) -> List[List[str]]:
        size = max(1, self._config.shard_size)
        return [self._tickers[i : i + size] for i in range(0, len(self._tickers), size)]

    async def _startup(self) -> None:
        loop = asyncio.get_running_loop()

        limits: Dict[str, Any] = {}
        costs: Dict[str, Any] = {}
        for name, fetch in (("limits", self._rest.account_limits), ("endpoint_costs", self._rest.endpoint_costs)):
            try:
                value = await loop.run_in_executor(None, fetch)
            except Exception as error:
                value = {"error": repr(error)}
            if name == "limits":
                limits = value
            else:
                costs = value

        self._resolution = await loop.run_in_executor(None, self._resolve_universe)
        if self._config.shard_count > 1:
            shards = universe_module.shard_by_series(
                self._resolution.markets, self._config.shard_count
            )
            self._shard_markets = shards[self._config.shard_index]
        else:
            self._shard_markets = list(self._resolution.markets)
        self._tickers = [m.ticker for m in self._shard_markets]
        if not self._tickers:
            raise RuntimeError(
                "shard %d of %d resolved to zero markets"
                % (self._config.shard_index, self._config.shard_count)
            )

        now = time.monotonic()
        for ticker in self._tickers:
            self._monitor.track_market(ticker, now)

        manifest = {
            "log_format_version": rec.LOG_FORMAT_VERSION,
            "session_id": self._session_id,
            "started_at_ms": int(time.time() * 1000),
            "environment": self._credentials.environment,
            "site": self._config.site,
            "shard_index": self._config.shard_index,
            "shard_count": self._config.shard_count,
            "universe_file": self._config.universe_file,
            "ws_url": self._transport.url,
            "rest_base": self._credentials.rest_base,
            "git_revision": git_revision(),
            "phase": 1,
            "config": {
                "shard_size": self._config.shard_size,
                "use_yes_price": self._config.use_yes_price,
                "channels": list(exchange.MARKET_DATA_CHANNELS),
                "stale_feed_seconds": self._config.stale_feed_seconds,
                "digest_interval_seconds": self._config.digest_interval_seconds,
            },
            "exchange_constants": {
                "verified_against_docs": exchange.VERIFIED_AGAINST_DOCS,
                "fee_schedule_effective": exchange.FEE_SCHEDULE_EFFECTIVE,
                "taker_fee_rate": str(exchange.TAKER_FEE_RATE),
                "maker_fee_rate": str(exchange.MAKER_FEE_RATE),
            },
            "account_limits": limits,
            "endpoint_costs": costs,
            "universe": {
                "count": len(self._tickers),
                "universe_total": len(self._resolution.markets),
                "per_vertical": self._resolution.per_vertical,
                "unmatched_categories": self._resolution.unmatched_categories,
                "categories_seen": self._resolution.categories_seen,
                "per_regime": self._resolution.per_regime,
                "fee_table_source": self._resolution.fee_table.source,
                "unrecognized_fee_types": sorted(self._resolution.fee_table.unrecognized_fee_types),
                "markets": [
                    {
                        "ticker": m.ticker,
                        "series_ticker": m.series_ticker,
                        "vertical": m.vertical,
                        "category": m.category,
                        "close_time": m.close_time,
                        "fee_type": m.fee_type,
                        "fee_multiplier": m.fee_multiplier,
                        "maker_multiplier": m.maker_multiplier,
                        "taker_multiplier": m.taker_multiplier,
                        "fee_regime": m.fee_regime,
                        "event_ticker": m.event_ticker,
                        "strike_type": m.strike_type,
                        "floor_strike": m.floor_strike,
                        "cap_strike": m.cap_strike,
                        "mutually_exclusive": m.mutually_exclusive,
                        "event_market_count": m.event_market_count,
                    }
                    for m in self._shard_markets
                ],
            },
        }
        write_session_manifest(self._recorder.directory, manifest)
        self._recorder.record_event(rec.KIND_SESSION_START, manifest)

    async def _subscribe_all(self) -> None:
        self._pending_subscriptions.clear()
        self._pending_resync.clear()
        for shard in self._shards():
            command_id = await self._transport.send_command(
                "subscribe",
                {
                    "channels": [BOOK_CHANNEL],
                    "market_tickers": shard,
                    "use_yes_price": self._config.use_yes_price,
                },
            )
            self._pending_subscriptions[command_id] = {"channel": BOOK_CHANNEL, "tickers": shard}
            self._recorder.record_event(
                rec.KIND_COMMAND,
                {"cmd": "subscribe", "id": command_id, "channel": BOOK_CHANNEL, "markets": len(shard)},
            )

        for channel in (TYPE_TRADE, TYPE_TICKER):
            for shard in self._shards():
                command_id = await self._transport.send_command(
                    "subscribe", {"channels": [channel], "market_tickers": shard}
                )
                self._pending_subscriptions[command_id] = {"channel": channel, "tickers": shard}
                self._recorder.record_event(
                    rec.KIND_COMMAND,
                    {"cmd": "subscribe", "id": command_id, "channel": channel, "markets": len(shard)},
                )

    async def _request_resync(self, sid: int, reason: str) -> None:
        sub = self._books.subscription(sid)
        if sub is None:
            return
        tickers = sub.invalid_tickers()
        if not tickers:
            return
        command_id = await self._transport.send_command(
            "update_subscription",
            {"sid": sid, "market_tickers": tickers, "action": "get_snapshot"},
        )
        self._pending_resync[sid] = time.monotonic()
        self._monitor.resync_requests += 1
        self._recorder.record_event(
            rec.KIND_RESYNC,
            {"sid": sid, "id": command_id, "reason": reason, "markets": len(tickers)},
        )

    async def _retry_stalled_resyncs(self, now: float) -> None:
        for sid, requested_at in list(self._pending_resync.items()):
            if now - requested_at < self._config.resync_retry_seconds:
                continue
            sub = self._books.subscription(sid)
            if sub is None or not sub.invalid_tickers():
                self._pending_resync.pop(sid, None)
                continue
            await self._request_resync(sid, "resync_timeout")

    def _emit_digest(self, mono: float) -> None:
        self._last_digest_mono = mono
        digests = {}
        for book in self._books.all_books():
            if book.state is BookState.VALID:
                digests[book.ticker] = book.digest()
        self._recorder.record_event(
            rec.KIND_BOOK_DIGEST,
            {
                "aggregate": self._books.digest(),
                "valid_books": len(digests),
                "total_books": self._books.total_count(),
                "digests": digests,
            },
        )

    def _emit_health(self, mono: float) -> None:
        self._last_health_mono = mono
        self._recorder.record_event(rec.KIND_HEALTH, self._monitor.snapshot(mono))

    async def _handle_message(self, message: ReceivedMessage) -> None:
        self._recorder.record_raw(message.raw, message.recv_ns, message.mono_ns)
        mono = message.mono_ns / 1e9
        self._monitor.last_message_mono = mono

        try:
            payload = json.loads(message.raw)
        except ValueError:
            self._monitor.integrity_errors += 1
            self._recorder.record_event(rec.KIND_INTEGRITY, {"error": "unparseable_json"})
            return

        message_type = payload.get("type")
        body = payload.get("msg") or {}
        self._monitor.on_message(
            message_type if isinstance(message_type, str) else "unknown",
            message.recv_ms,
            body.get("ts_ms") if isinstance(body, dict) else None,
            message.size,
        )

        if message_type == TYPE_DELTA or message_type == TYPE_SNAPSHOT:
            await self._handle_book_message(message_type, payload, body, mono)
        elif message_type == TYPE_SUBSCRIBED:
            self._handle_subscribed(payload, body, mono)
        elif message_type == TYPE_OK:
            self._handle_ok(payload, body)
        elif message_type == TYPE_ERROR:
            self._recorder.record_event(rec.KIND_INTEGRITY, {"error": "ws_error", "payload": payload})
        elif message_type not in KNOWN_TYPES:
            self._monitor.integrity_errors += 1
            self._recorder.record_event(
                rec.KIND_INTEGRITY, {"error": "unknown_message_type", "type": message_type}
            )

    def _handle_subscribed(self, payload: dict, body: dict, mono: float) -> None:
        command_id = payload.get("id")
        sid = body.get("sid")
        channel = body.get("channel")
        pending = self._pending_subscriptions.pop(command_id, None)
        tickers = pending["tickers"] if pending else []
        if channel == BOOK_CHANNEL and isinstance(sid, int):
            self._books.register(sid, channel, tickers)
        self._recorder.record_event(
            rec.KIND_SUBSCRIBED,
            {"sid": sid, "channel": channel, "id": command_id, "tickers": tickers},
        )

    def _handle_ok(self, payload: dict, body: dict) -> None:
        sid = payload.get("sid")
        tickers = body.get("market_tickers") if isinstance(body, dict) else None
        if isinstance(sid, int) and isinstance(tickers, list):
            sub = self._books.subscription(sid)
            if sub is not None:
                for ticker in tickers:
                    sub.book(ticker)

    async def _handle_book_message(
        self, message_type: str, payload: dict, body: dict, mono: float
    ) -> None:
        sid = payload.get("sid")
        seq = payload.get("seq")
        ticker = body.get("market_ticker")
        if not isinstance(sid, int) or not isinstance(seq, int) or not isinstance(ticker, str):
            self._monitor.integrity_errors += 1
            self._recorder.record_event(
                rec.KIND_INTEGRITY, {"error": "malformed_book_envelope", "payload": payload}
            )
            return

        sub = self._books.subscription(sid)
        if sub is None:
            sub = self._books.register(sid, BOOK_CHANNEL, [ticker])

        gap = sub.observe_seq(seq)
        if gap is not None:
            await self._on_gap(gap, sub, mono)

        book = sub.book(ticker)
        self._monitor.track_market(ticker, mono)

        try:
            if message_type == TYPE_SNAPSHOT:
                book.apply_snapshot(body, seq)
                self._monitor.mark_valid(ticker, mono)
                if not sub.invalid_tickers():
                    self._pending_resync.pop(sid, None)
            else:
                if book.state is not BookState.VALID:
                    return
                book.apply_delta(body, seq)
        except BookIntegrityError as error:
            self._monitor.integrity_errors += 1
            book.invalidate()
            self._monitor.mark_invalid(ticker, mono)
            self._recorder.record_event(
                rec.KIND_INTEGRITY,
                {"error": "book_integrity", "detail": str(error), "ticker": ticker, "sid": sid},
            )
            await self._request_resync(sid, "book_integrity")

    async def _on_gap(self, gap: GapEvent, sub, mono: float) -> None:
        self._monitor.on_gap(gap.kind.value, gap.missed)
        for ticker in list(sub.books):
            self._monitor.mark_invalid(ticker, mono)
        sub.invalidate_all()
        self._recorder.record_event(
            rec.KIND_GAP,
            {
                "sid": gap.sid,
                "kind": gap.kind.value,
                "expected_seq": gap.expected_seq,
                "observed_seq": gap.observed_seq,
                "missed": gap.missed,
                "markets": len(sub.books),
            },
        )
        await self._request_resync(gap.sid, "sequence_gap")

    async def _consume(self) -> None:
        async for message in self._transport.messages():
            await self._handle_message(message)
            now = time.monotonic()
            if now - self._last_digest_mono >= self._config.digest_interval_seconds:
                self._emit_digest(now)
            if now - self._last_health_mono >= self._config.health_interval_seconds:
                self._emit_health(now)
            if self._pending_resync:
                await self._retry_stalled_resyncs(now)
            if self._stop.is_set():
                return

    async def _watchdog(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(1.0)
                now = time.monotonic()
                last = self._monitor.last_message_mono
                if last is not None and now - last > self._config.stale_feed_seconds:
                    self._recorder.record_event(
                        rec.KIND_CONNECTION,
                        {"event": "stale_feed", "seconds_since_last_message": now - last},
                    )
                    return
                if (
                    self._config.max_session_seconds is not None
                    and now - self._started_mono >= self._config.max_session_seconds
                ):
                    self._stop.set()
                    return
        finally:
            await self._transport.close()

    async def _session(self) -> None:
        await self._transport.open()
        self._policy.reset()
        self._monitor.last_message_mono = time.monotonic()
        self._recorder.record_event(rec.KIND_CONNECTION, {"event": "connected", "url": self._transport.url})
        await self._subscribe_all()
        watchdog = asyncio.ensure_future(self._watchdog())
        try:
            await self._consume()
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except (asyncio.CancelledError, Exception):
                pass
            await self._transport.close()

    async def run(self) -> Dict[str, Any]:
        await self._startup()
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                except Exception as error:
                    self._recorder.record_event(
                        rec.KIND_CONNECTION,
                        {"event": "disconnected", "error": repr(error)},
                    )
                if self._stop.is_set():
                    break
                now = time.monotonic()
                for ticker in list(self._monitor.markets):
                    self._monitor.mark_invalid(ticker, now)
                self._books.reset()
                self._monitor.reconnects += 1
                delay = self._policy.next_delay()
                self._recorder.record_event(
                    rec.KIND_CONNECTION,
                    {"event": "reconnect_scheduled", "delay_seconds": delay, "attempt": self._policy.attempt},
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            now = time.monotonic()
            self._emit_digest(now)
            report = self._monitor.final_report(now)
            self._recorder.record_event(rec.KIND_SESSION_END, report)
            self._recorder.flush()
            self._recorder.close()
        return report


async def run_capture(config: CaptureConfig) -> Dict[str, Any]:
    daemon = CaptureDaemon(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            pass
    return await daemon.run()

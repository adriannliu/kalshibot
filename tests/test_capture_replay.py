from __future__ import annotations

import asyncio
import json
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.asyncio.server import serve

from auth.credentials import Credentials
from config.exchange import FeeScheduleTable
from ops.capture_daemon import CaptureConfig, CaptureDaemon
from feed.universe import SelectedMarket, UniverseResolution
from research.replay import replay_session

TICKERS = ["TEST-A", "TEST-B"]

SNAPSHOT_A = {
    "market_ticker": "TEST-A",
    "yes_dollars_fp": [["0.4000", "100.00"], ["0.4100", "50.00"]],
    "no_dollars_fp": [["0.5500", "80.00"]],
}
SNAPSHOT_B = {
    "market_ticker": "TEST-B",
    "yes_dollars_fp": [["0.1000", "500.00"]],
    "no_dollars_fp": [["0.8500", "300.00"], ["0.8600", "20.00"]],
}


def envelope(message_type, sid, seq, msg):
    payload = {"type": message_type, "sid": sid, "msg": msg}
    if seq is not None:
        payload["seq"] = seq
    return json.dumps(payload)


def delta(ticker, side, price, amount, ts_ms=1750000000000):
    return {
        "market_ticker": ticker,
        "price_dollars": price,
        "delta_fp": amount,
        "side": side,
        "ts_ms": ts_ms,
    }


class ScriptedServer:
    def __init__(self):
        self.gap_resync_requested = asyncio.Event()
        self.finished = asyncio.Event()
        self.resync_tickers = None

    async def handler(self, connection):
        sids = {}
        next_sid = 1
        for _ in range(3):
            command = json.loads(await connection.recv())
            channel = command["params"]["channels"][0]
            sids[channel] = next_sid
            await connection.send(
                json.dumps(
                    {
                        "type": "subscribed",
                        "id": command["id"],
                        "msg": {"channel": channel, "sid": next_sid},
                    }
                )
            )
            next_sid += 1

        book_sid = sids["orderbook_delta"]
        trade_sid = sids["trade"]

        await connection.send(envelope("orderbook_snapshot", book_sid, 1, SNAPSHOT_A))
        await connection.send(envelope("orderbook_snapshot", book_sid, 2, SNAPSHOT_B))
        await connection.send(
            envelope("orderbook_delta", book_sid, 3, delta("TEST-A", "yes", "0.4000", "-25.00"))
        )
        await connection.send(
            envelope("orderbook_delta", book_sid, 4, delta("TEST-B", "no", "0.8600", "-20.00"))
        )
        await connection.send(
            envelope("orderbook_delta", book_sid, 6, delta("TEST-A", "yes", "0.3900", "10.00"))
        )

        while True:
            command = json.loads(await connection.recv())
            if command.get("cmd") == "update_subscription" and command["params"].get("action") == "get_snapshot":
                self.resync_tickers = sorted(command["params"]["market_tickers"])
                self.gap_resync_requested.set()
                break

        await connection.send(envelope("orderbook_snapshot", book_sid, 7, SNAPSHOT_A))
        await connection.send(envelope("orderbook_snapshot", book_sid, 8, SNAPSHOT_B))
        await connection.send(
            envelope("orderbook_delta", book_sid, 9, delta("TEST-A", "no", "0.5500", "15.00"))
        )
        await connection.send(
            envelope("orderbook_delta", book_sid, 10, delta("TEST-B", "yes", "0.1000", "-100.00"))
        )
        await connection.send(
            json.dumps(
                {
                    "type": "trade",
                    "sid": trade_sid,
                    "msg": {
                        "trade_id": "t-1",
                        "market_ticker": "TEST-A",
                        "yes_price_dollars": "0.400",
                        "no_price_dollars": "0.600",
                        "count_fp": "10.00",
                        "taker_outcome_side": "yes",
                        "taker_book_side": "bid",
                        "is_block_trade": False,
                        "ts_ms": 1750000000500,
                    },
                }
            )
        )
        self.finished.set()
        await connection.wait_closed()


def make_credentials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return Credentials(key_id="test-key", private_key_pem=pem, environment="demo")


def fake_resolution():
    return UniverseResolution(
        markets=[
            SelectedMarket(
                ticker=ticker,
                series_ticker="TEST",
                vertical="test",
                category="Test",
                close_time=None,
                volume_fp=0,
                fee_type="quadratic",
                fee_multiplier="1",
                maker_multiplier="0",
                taker_multiplier="1",
                fee_regime="maker_free",
            )
            for ticker in TICKERS
        ],
        fee_table=FeeScheduleTable(schedules={}, source="test", fetched_at_ms=0),
        per_vertical={"test": 2},
        per_regime={"maker_free": 2},
    )


class StubRest:
    def account_limits(self):
        return {"stub": True}

    def endpoint_costs(self):
        return {"stub": True}


@pytest.mark.asyncio
async def test_capture_survives_a_gap_and_replays_byte_identically(tmp_path):
    server_script = ScriptedServer()
    async with serve(server_script.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        config = CaptureConfig(
            data_root=str(tmp_path),
            session_id="e2e",
            shard_size=10,
            digest_interval_seconds=0.0,
            health_interval_seconds=0.0,
            resync_retry_seconds=2.0,
            stale_feed_seconds=30.0,
            max_session_seconds=3.0,
        )
        daemon = CaptureDaemon(config, credentials=make_credentials())
        daemon._rest = StubRest()
        daemon._resolve_universe = fake_resolution
        daemon._transport._url = "ws://127.0.0.1:%d/trade-api/ws/v2" % port

        report = await asyncio.wait_for(daemon.run(), timeout=20)

    assert server_script.gap_resync_requested.is_set()
    assert server_script.resync_tickers == sorted(TICKERS)
    assert server_script.finished.is_set()

    assert report["gaps"] == {"missed": 1}
    assert report["missed_messages"] == 1
    assert report["integrity_errors"] == 0
    assert report["messages"]["orderbook_snapshot"] == 4
    assert report["messages"]["orderbook_delta"] == 5
    assert report["messages"]["trade"] == 1
    assert report["clock_offset"]["count"] > 0

    session_dir = os.path.join(str(tmp_path), "e2e")
    result = replay_session(session_dir, strict_ordinals=True)

    assert result.verified
    assert result.digest_mismatches == []
    assert result.aggregate_mismatches == 0
    assert result.gaps_live == 1
    assert result.gaps_replayed == 1
    assert result.digests_checked > 5
    assert result.integrity_errors == 0


@pytest.mark.asyncio
async def test_manifest_records_capture_provenance(tmp_path):
    server_script = ScriptedServer()
    async with serve(server_script.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = CaptureConfig(
            data_root=str(tmp_path),
            session_id="manifest",
            shard_size=10,
            digest_interval_seconds=1.0,
            health_interval_seconds=1.0,
            max_session_seconds=2.0,
        )
        daemon = CaptureDaemon(config, credentials=make_credentials())
        daemon._rest = StubRest()
        daemon._resolve_universe = fake_resolution
        daemon._transport._url = "ws://127.0.0.1:%d/trade-api/ws/v2" % port
        await asyncio.wait_for(daemon.run(), timeout=20)

    with open(os.path.join(str(tmp_path), "manifest", "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)

    assert manifest["phase"] == 1
    assert manifest["universe"]["count"] == 2
    assert manifest["config"]["use_yes_price"] is False
    assert manifest["log_format_version"] == 1
    assert manifest["exchange_constants"]["maker_fee_rate"] == "0.0175"


class DroppingServer:
    def __init__(self):
        self.connections = 0

    async def handler(self, connection):
        self.connections += 1
        attempt = self.connections
        sids = {}
        next_sid = 1
        for _ in range(3):
            command = json.loads(await connection.recv())
            channel = command["params"]["channels"][0]
            sids[channel] = next_sid
            await connection.send(
                json.dumps(
                    {
                        "type": "subscribed",
                        "id": command["id"],
                        "msg": {"channel": channel, "sid": next_sid},
                    }
                )
            )
            next_sid += 1

        book_sid = sids["orderbook_delta"]
        await connection.send(envelope("orderbook_snapshot", book_sid, 1, SNAPSHOT_A))
        await connection.send(envelope("orderbook_snapshot", book_sid, 2, SNAPSHOT_B))
        await connection.send(
            envelope("orderbook_delta", book_sid, 3, delta("TEST-A", "yes", "0.4000", "-25.00"))
        )

        if attempt == 1:
            await asyncio.sleep(0.2)
            await connection.close()
            return

        await connection.send(
            envelope("orderbook_delta", book_sid, 4, delta("TEST-B", "yes", "0.1000", "-100.00"))
        )
        await connection.wait_closed()


@pytest.mark.asyncio
async def test_reconnect_rebuilds_from_snapshot_and_replays_clean(tmp_path):
    script = DroppingServer()
    async with serve(script.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = CaptureConfig(
            data_root=str(tmp_path),
            session_id="reconnect",
            shard_size=10,
            digest_interval_seconds=0.0,
            health_interval_seconds=0.0,
            max_session_seconds=3.0,
        )
        daemon = CaptureDaemon(config, credentials=make_credentials())
        daemon._rest = StubRest()
        daemon._resolve_universe = fake_resolution
        daemon._transport._url = "ws://127.0.0.1:%d/trade-api/ws/v2" % port
        daemon._policy = type(daemon._policy)(base_seconds=0.05, max_seconds=0.05, jitter=0.0)
        report = await asyncio.wait_for(daemon.run(), timeout=20)

    assert script.connections >= 2
    assert report["reconnects"] >= 1
    assert report["integrity_errors"] == 0
    assert report["gaps"] == {}

    result = replay_session(os.path.join(str(tmp_path), "reconnect"), strict_ordinals=True)
    assert result.verified
    assert result.digest_mismatches == []
    assert result.aggregate_mismatches == 0


class SilentServer:
    async def handler(self, connection):
        for _ in range(3):
            command = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "type": "subscribed",
                        "id": command["id"],
                        "msg": {"channel": command["params"]["channels"][0], "sid": 1},
                    }
                )
            )
        await connection.wait_closed()


@pytest.mark.asyncio
async def test_external_stop_shuts_down_promptly_on_a_silent_feed(tmp_path):
    async with serve(SilentServer().handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = CaptureConfig(
            data_root=str(tmp_path),
            session_id="sigterm",
            shard_size=10,
            stale_feed_seconds=3600.0,
            max_session_seconds=None,
        )
        daemon = CaptureDaemon(config, credentials=make_credentials())
        daemon._rest = StubRest()
        daemon._resolve_universe = fake_resolution
        daemon._transport._url = "ws://127.0.0.1:%d/trade-api/ws/v2" % port

        task = asyncio.ensure_future(daemon.run())
        await asyncio.sleep(1.5)
        daemon.request_stop()
        report = await asyncio.wait_for(task, timeout=10)

    assert report["messages"].get("orderbook_delta") is None
    assert os.path.exists(os.path.join(str(tmp_path), "sigterm", "manifest.json"))

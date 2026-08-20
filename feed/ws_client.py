from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

import websockets
from websockets.asyncio.client import ClientConnection, connect

from auth.credentials import Credentials
from auth.signer import RequestSigner

USER_AGENT = "kalshi-mm-phase1/0.1"


@dataclass(frozen=True)
class ReceivedMessage:
    raw: str
    recv_ns: int
    mono_ns: int

    @property
    def recv_ms(self) -> int:
        return self.recv_ns // 1_000_000

    @property
    def size(self) -> int:
        return len(self.raw)


class ReconnectPolicy:
    def __init__(
        self,
        base_seconds: float = 0.5,
        factor: float = 2.0,
        max_seconds: float = 60.0,
        jitter: float = 0.3,
    ) -> None:
        self._base = base_seconds
        self._factor = factor
        self._max = max_seconds
        self._jitter = jitter
        self._attempt = 0
        self._random = random.Random()

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self._base * (self._factor ** self._attempt), self._max)
        self._attempt += 1
        spread = delay * self._jitter
        return max(0.0, delay + self._random.uniform(-spread, spread))

    @property
    def attempt(self) -> int:
        return self._attempt


class WebSocketTransport:
    def __init__(
        self,
        credentials: Credentials,
        signer: RequestSigner,
        ping_interval: Optional[float] = 20.0,
        ping_timeout: Optional[float] = 10.0,
        max_queue: int = 8192,
        max_size: int = 16 * 1024 * 1024,
        open_timeout: float = 20.0,
    ) -> None:
        self._url = credentials.ws_url
        self._signer = signer
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_queue = max_queue
        self._max_size = max_size
        self._open_timeout = open_timeout
        self._connection: Optional[ClientConnection] = None
        self._command_id = 0

    @property
    def url(self) -> str:
        return self._url

    async def open(self) -> ClientConnection:
        headers = self._signer.ws_headers()
        headers["User-Agent"] = USER_AGENT
        self._connection = await connect(
            self._url,
            additional_headers=headers,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_queue=self._max_queue,
            max_size=self._max_size,
            open_timeout=self._open_timeout,
        )
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            finally:
                self._connection = None

    def next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

    async def send_command(self, cmd: str, params: Dict[str, Any]) -> int:
        if self._connection is None:
            raise RuntimeError("websocket is not connected")
        command_id = self.next_command_id()
        await self._connection.send(
            json.dumps({"id": command_id, "cmd": cmd, "params": params}, separators=(",", ":"))
        )
        return command_id

    async def messages(self) -> AsyncIterator[ReceivedMessage]:
        if self._connection is None:
            raise RuntimeError("websocket is not connected")
        async for frame in self._connection:
            recv_ns = time.time_ns()
            mono_ns = time.monotonic_ns()
            raw = frame if isinstance(frame, str) else frame.decode("utf-8")
            yield ReceivedMessage(raw=raw, recv_ns=recv_ns, mono_ns=mono_ns)

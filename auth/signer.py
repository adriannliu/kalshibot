from __future__ import annotations

import base64
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from auth.credentials import Credentials
from config import exchange


class InvalidPrivateKey(RuntimeError):
    pass


def strip_query(path: str) -> str:
    return urlsplit(path).path


def now_timestamp_ms() -> int:
    return int(time.time() * 1000)


class RequestSigner:
    def __init__(self, credentials: Credentials) -> None:
        self._key_id = credentials.key_id
        key = serialization.load_pem_private_key(credentials.private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise InvalidPrivateKey("Kalshi requires an RSA private key")
        self._key = key

    @property
    def key_id(self) -> str:
        return self._key_id

    def signature(self, method: str, path: str, timestamp_ms: int) -> str:
        message = ("%d%s%s" % (timestamp_ms, method.upper(), strip_query(path))).encode("utf-8")
        signed = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signed).decode("utf-8")

    def headers(
        self,
        method: str,
        path: str,
        timestamp_ms: Optional[int] = None,
    ) -> Dict[str, str]:
        stamp = now_timestamp_ms() if timestamp_ms is None else timestamp_ms
        return {
            exchange.HEADER_ACCESS_KEY: self._key_id,
            exchange.HEADER_ACCESS_TIMESTAMP: str(stamp),
            exchange.HEADER_ACCESS_SIGNATURE: self.signature(method, path, stamp),
        }

    def rest_headers(self, method: str, endpoint: str, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        return self.headers(method, exchange.REST_SIGN_PREFIX + endpoint, timestamp_ms)

    def ws_headers(self, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        return self.headers("GET", exchange.WS_SIGN_PATH, timestamp_ms)

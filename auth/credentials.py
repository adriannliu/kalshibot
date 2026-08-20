from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from config import exchange

ENV_KEY_ID = "KALSHI_API_KEY_ID"
ENV_PRIVATE_KEY_PEM = "KALSHI_PRIVATE_KEY_PEM"
ENV_PRIVATE_KEY_PATH = "KALSHI_PRIVATE_KEY_PATH"
ENV_ENVIRONMENT = "KALSHI_ENV"

ENVIRONMENT_PROD = "prod"
ENVIRONMENT_DEMO = "demo"


class MissingCredentials(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    key_id: str
    private_key_pem: bytes
    environment: str

    @property
    def rest_base(self) -> str:
        if self.environment == ENVIRONMENT_PROD:
            return exchange.REST_BASE_PROD
        return exchange.REST_BASE_DEMO

    @property
    def ws_url(self) -> str:
        if self.environment == ENVIRONMENT_PROD:
            return exchange.WS_URL_PROD
        return exchange.WS_URL_DEMO

    def __repr__(self) -> str:
        return "Credentials(key_id=%s..., environment=%s)" % (
            self.key_id[:6],
            self.environment,
        )


def load_from_env(environ: Optional[dict] = None) -> Credentials:
    env = os.environ if environ is None else environ

    key_id = env.get(ENV_KEY_ID, "").strip()
    if not key_id:
        raise MissingCredentials("%s is not set" % ENV_KEY_ID)

    pem_inline = env.get(ENV_PRIVATE_KEY_PEM)
    pem_path = env.get(ENV_PRIVATE_KEY_PATH)

    if pem_inline:
        private_key_pem = pem_inline.encode("utf-8")
    elif pem_path:
        with open(pem_path, "rb") as handle:
            private_key_pem = handle.read()
    else:
        raise MissingCredentials(
            "one of %s or %s must be set" % (ENV_PRIVATE_KEY_PEM, ENV_PRIVATE_KEY_PATH)
        )

    environment = env.get(ENV_ENVIRONMENT, ENVIRONMENT_DEMO).strip().lower()
    if environment not in (ENVIRONMENT_PROD, ENVIRONMENT_DEMO):
        raise MissingCredentials("%s must be %s or %s" % (ENV_ENVIRONMENT, ENVIRONMENT_PROD, ENVIRONMENT_DEMO))

    return Credentials(key_id=key_id, private_key_pem=private_key_pem, environment=environment)

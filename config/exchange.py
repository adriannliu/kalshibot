from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from typing import Dict, FrozenSet, Optional, Set

VERIFIED_AGAINST_DOCS = "2026-08-19"
FEE_SCHEDULE_EFFECTIVE = "2026-07-07"
FEE_SCHEDULE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"

REST_BASE_PROD = "https://external-api.kalshi.com/trade-api/v2"
REST_BASE_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
WS_URL_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_URL_DEMO = "wss://external-api.demo.kalshi.co/trade-api/ws/v2"

REST_SIGN_PREFIX = "/trade-api/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

HEADER_ACCESS_KEY = "KALSHI-ACCESS-KEY"
HEADER_ACCESS_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_ACCESS_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"

SETTLEMENT_VALUE = Decimal("1")
CENTICENT = Decimal("0.0001")
COUNT_DECIMALS = 2
COUNT_SCALE = 10 ** COUNT_DECIMALS

TAKER_FEE_RATE = Decimal("0.07")
MAKER_FEE_RATE = Decimal("0.0175")
TAKER_MULTIPLIER_DEFAULT = Decimal("1")
MAKER_MULTIPLIER_DEFAULT = Decimal("0")
MAKER_MULTIPLIER_WHEN_UNKNOWN = Decimal("1")

VOLUME_INCENTIVE_CAP_PER_CONTRACT = Decimal("0.005")
VOLUME_INCENTIVE_PRICE_MIN = Decimal("0.03")
VOLUME_INCENTIVE_PRICE_MAX = Decimal("0.97")
INCENTIVE_TERMS_REVIEW_DATE = "2026-09-01"

SERVER_PING_INTERVAL_S = 10

MARKET_DATA_CHANNELS = ("orderbook_delta", "ticker", "trade")
ORDERBOOK_CHANNEL = "orderbook_delta"

USE_YES_PRICE = False

MARKETS_PER_SUBSCRIPTION = 20

WS_ERROR_SUBSCRIPTION_MARKET_LIMIT = 26
WS_ERROR_SUBSCRIPTION_BUFFER_OVERFLOW = 25
WS_ERROR_TOO_MANY_REQUESTS = 27

ENDPOINT_LIMITS = "/account/limits"
ENDPOINT_ENDPOINT_COSTS = "/account/endpoint_costs"
ENDPOINT_FEE_TIERS = "/margin/fee_tiers"
ENDPOINT_MARKETS = "/markets"
ENDPOINT_ORDERBOOK = "/markets/{ticker}/orderbook"


def round_up_centicent(amount: Decimal) -> Decimal:
    return amount.quantize(CENTICENT, rounding=ROUND_UP)


def contracts_from_fp(count_fp: int) -> Decimal:
    return Decimal(count_fp) / COUNT_SCALE


def series_ticker_of(market_ticker: str) -> str:
    return market_ticker.split("-", 1)[0]


def taker_fee(
    price: Decimal,
    count_fp: int,
    multiplier: Decimal = TAKER_MULTIPLIER_DEFAULT,
) -> Decimal:
    contracts = contracts_from_fp(count_fp)
    return round_up_centicent(
        multiplier * TAKER_FEE_RATE * contracts * price * (SETTLEMENT_VALUE - price)
    )


def maker_fee(price: Decimal, count_fp: int, multiplier: Decimal) -> Decimal:
    contracts = contracts_from_fp(count_fp)
    return round_up_centicent(
        multiplier * MAKER_FEE_RATE * contracts * price * (SETTLEMENT_VALUE - price)
    )


def maker_round_trip_cost_per_contract(price: Decimal, multiplier: Decimal) -> Decimal:
    return 2 * multiplier * MAKER_FEE_RATE * price * (SETTLEMENT_VALUE - price)


class UnknownSeriesFeeSchedule(LookupError):
    pass


FEE_TYPE_QUADRATIC = "quadratic"
FEE_TYPE_QUADRATIC_WITH_MAKER = "quadratic_with_maker_fees"
FEE_TYPE_QUADRATIC_WITH_COMBO_MAKER = "quadratic_with_combo_maker_fees"

FEE_TYPES_WITHOUT_MAKER_FEES = frozenset({FEE_TYPE_QUADRATIC})
KNOWN_FEE_TYPES = frozenset(
    {
        FEE_TYPE_QUADRATIC,
        FEE_TYPE_QUADRATIC_WITH_MAKER,
        FEE_TYPE_QUADRATIC_WITH_COMBO_MAKER,
    }
)

REGIME_MAKER_CHARGED = "maker_charged"
REGIME_MAKER_FREE = "maker_free"
REGIME_ZERO_FEE = "zero_fee"
REGIME_ANY = "any"


@dataclass(frozen=True)
class SeriesFeeSchedule:
    series_ticker: str
    fee_type: str
    fee_multiplier: Decimal

    @property
    def recognized(self) -> bool:
        return self.fee_type in KNOWN_FEE_TYPES

    @property
    def taker_multiplier(self) -> Decimal:
        return self.fee_multiplier

    @property
    def maker_multiplier(self) -> Decimal:
        if self.fee_type in FEE_TYPES_WITHOUT_MAKER_FEES:
            return MAKER_MULTIPLIER_DEFAULT
        return self.fee_multiplier

    @property
    def charges_maker_fees(self) -> bool:
        return self.maker_multiplier > 0

    @property
    def regime(self) -> str:
        if self.fee_multiplier == 0:
            return REGIME_ZERO_FEE
        if self.charges_maker_fees:
            return REGIME_MAKER_CHARGED
        return REGIME_MAKER_FREE

    def maker_round_trip_cost(self, price: Decimal) -> Decimal:
        return maker_round_trip_cost_per_contract(price, self.maker_multiplier)


@dataclass(frozen=True)
class FeeScheduleTable:
    schedules: Dict[str, SeriesFeeSchedule]
    source: str
    fetched_at_ms: int
    unrecognized_fee_types: FrozenSet[str] = frozenset()

    def is_known(self, series: str) -> bool:
        return series in self.schedules

    def get(self, series: str) -> Optional[SeriesFeeSchedule]:
        return self.schedules.get(series)

    def schedule_for(self, series: str) -> SeriesFeeSchedule:
        schedule = self.schedules.get(series)
        if schedule is None:
            raise UnknownSeriesFeeSchedule(series)
        return schedule

    def maker_multiplier(self, series: str) -> Decimal:
        schedule = self.schedules.get(series)
        if schedule is None:
            return MAKER_MULTIPLIER_WHEN_UNKNOWN
        return schedule.maker_multiplier

    def taker_multiplier(self, series: str) -> Decimal:
        schedule = self.schedules.get(series)
        if schedule is None:
            return TAKER_MULTIPLIER_DEFAULT
        return schedule.taker_multiplier

    def regime(self, series: str) -> str:
        schedule = self.schedules.get(series)
        if schedule is None:
            return REGIME_MAKER_CHARGED
        return schedule.regime


@dataclass(frozen=True)
class ConservativeFeeSchedule:
    reason: str

    def is_known(self, series: str) -> bool:
        return False

    def maker_multiplier(self, series: str) -> Decimal:
        return MAKER_MULTIPLIER_WHEN_UNKNOWN

    def taker_multiplier(self, series: str) -> Decimal:
        return TAKER_MULTIPLIER_DEFAULT

    def regime(self, series: str) -> str:
        return REGIME_MAKER_CHARGED

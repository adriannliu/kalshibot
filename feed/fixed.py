from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Union

from config.exchange import COUNT_DECIMALS, COUNT_SCALE


class FixedPointError(ValueError):
    pass


def parse_price(text: Union[str, Decimal]) -> Decimal:
    if isinstance(text, Decimal):
        return text
    if not isinstance(text, str):
        raise FixedPointError("price must be a fixed-point string, got %r" % (text,))
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise FixedPointError("unparseable price %r" % (text,))
    if not value.is_finite():
        raise FixedPointError("non-finite price %r" % (text,))
    return value


def parse_count_fp(text: Union[str, int]) -> int:
    if isinstance(text, int):
        raise FixedPointError("count must be a fixed-point string, got bare int %r" % (text,))
    if not isinstance(text, str):
        raise FixedPointError("count must be a fixed-point string, got %r" % (text,))
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise FixedPointError("unparseable count %r" % (text,))
    if not value.is_finite():
        raise FixedPointError("non-finite count %r" % (text,))
    scaled = value.scaleb(COUNT_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise FixedPointError("count %r has finer resolution than %d decimals" % (text, COUNT_DECIMALS))
    return int(scaled)


def format_count_fp(count_fp: int) -> str:
    return str(Decimal(count_fp).scaleb(-COUNT_DECIMALS).quantize(Decimal(1).scaleb(-COUNT_DECIMALS)))


def format_price(price: Decimal) -> str:
    return format(price.normalize(), "f")


def contracts(count_fp: int) -> Decimal:
    return Decimal(count_fp) / COUNT_SCALE

from decimal import Decimal

import pytest

from feed.fixed import FixedPointError, format_count_fp, parse_count_fp, parse_price


def test_counts_parse_to_scaled_integers():
    assert parse_count_fp("13.00") == 1300
    assert parse_count_fp("-54.00") == -5400
    assert parse_count_fp("0.01") == 1


def test_count_round_trips():
    assert format_count_fp(parse_count_fp("136.00")) == "136.00"


def test_bare_numbers_are_rejected():
    with pytest.raises(FixedPointError):
        parse_count_fp(13)
    with pytest.raises(FixedPointError):
        parse_count_fp(13.0)


def test_finer_resolution_than_two_decimals_is_rejected():
    with pytest.raises(FixedPointError):
        parse_count_fp("1.005")


def test_prices_stay_exact_decimals():
    price = parse_price("0.0800")
    assert isinstance(price, Decimal)
    assert price == Decimal("0.08")
    assert parse_price("0.960") + parse_price("0.030") == Decimal("0.99")


def test_price_dict_keys_are_scale_insensitive():
    levels = {parse_price("0.0800"): 1}
    assert levels[parse_price("0.08")] == 1

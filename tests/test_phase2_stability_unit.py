from __future__ import annotations

from research import phase2_report as p2


def test_daily_market_churn_destroys_market_level_overlap():
    monday = ["KXHIGHNY-26AUG24-B89", "KXHIGHCHI-26AUG24-B77", "KXBTCD-26AUG24-T60000"]
    tuesday = ["KXHIGHNY-26AUG25-B91", "KXHIGHCHI-26AUG25-B75", "KXBTCD-26AUG25-T61000"]

    assert p2._overlap(monday, tuesday) == 0.0
    assert p2._series_overlap(monday, tuesday) == 1.0


def test_series_overlap_still_detects_a_genuinely_different_set():
    week_one = ["KXHIGHNY-26AUG24-B89", "KXHIGHCHI-26AUG24-B77"]
    week_two = ["KXNFLGAME-26AUG25-DAL", "KXCPI-26AUG25-T3"]

    assert p2._series_overlap(week_one, week_two) == 0.0


def test_partial_series_agreement_scores_between():
    a = ["KXHIGHNY-1-A", "KXHIGHCHI-1-A"]
    b = ["KXHIGHNY-2-B", "KXCPI-2-B"]

    assert p2._series_overlap(a, b) == 1 / 3


def test_empty_windows_score_zero():
    assert p2._series_overlap([], ["KXHIGHNY-1-A"]) == 0.0

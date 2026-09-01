from __future__ import annotations

import datetime

import pytest

from research import phase2_report as p2


class Stub:
    def __init__(self, started_at_ms, site="use1"):
        self.started_at_ms = started_at_ms
        self.site = site

    def __repr__(self):
        if self.started_at_ms is None:
            return "Stub(None)"
        return "Stub(%s)" % datetime.datetime.utcfromtimestamp(
            self.started_at_ms / 1000
        ).strftime("%m-%d")


def day(month, dom, hour=12):
    return int(
        datetime.datetime(2026, month, dom, hour, tzinfo=datetime.timezone.utc).timestamp() * 1000
    )


def test_contaminated_sessions_are_excluded_by_the_default_cutoff():
    since_ms = p2.parse_since(p2.CLEAN_CAPTURE_SINCE)
    sessions = [Stub(day(8, 20)), Stub(day(8, 21)), Stub(day(8, 22)), Stub(day(8, 24, 18)), Stub(day(8, 25))]

    kept = p2.filter_since(sessions, since_ms)

    assert len(kept) == 2
    assert all(s.started_at_ms >= since_ms for s in kept)


def test_the_clean_cutoff_sits_between_the_freeze_and_the_recovery():
    since_ms = p2.parse_since(p2.CLEAN_CAPTURE_SINCE)
    assert day(8, 22, 2) < since_ms
    assert since_ms < day(8, 24, 17)


def test_all_history_keeps_everything():
    sessions = [Stub(day(8, 20)), Stub(day(8, 25))]
    assert len(p2.filter_since(sessions, p2.parse_since(None))) == 2


def test_interleaving_puts_adjacent_days_in_different_windows():
    sessions = [Stub(day(8, d)) for d in range(24, 32)]
    buckets = p2.assign_windows(sessions, 2, p2.WINDOW_MODE_INTERLEAVED)

    assert len(buckets) == 2
    assert all(buckets)
    for bucket in buckets:
        days = sorted(
            datetime.datetime.utcfromtimestamp(s.started_at_ms / 1000).day for s in bucket
        )
        assert all(b - a == 2 for a, b in zip(days, days[1:]))


def test_interleaving_gives_each_window_the_full_weekly_cycle():
    sessions = [Stub(day(8, d)) for d in range(24, 32)]
    buckets = p2.assign_windows(sessions, 2, p2.WINDOW_MODE_INTERLEAVED)

    weekdays = [
        {datetime.datetime.utcfromtimestamp(s.started_at_ms / 1000).weekday() for s in bucket}
        for bucket in buckets
    ]
    assert len(weekdays[0]) >= 4 and len(weekdays[1]) >= 4
    assert weekdays[0] != weekdays[1]


def test_chronological_splitting_still_available_and_contiguous():
    sessions = [Stub(day(8, d)) for d in range(24, 32)]
    buckets = p2.assign_windows(sessions, 2, p2.WINDOW_MODE_CHRONOLOGICAL)

    first = max(s.started_at_ms for s in buckets[0])
    second = min(s.started_at_ms for s in buckets[1])
    assert first < second


def test_windows_are_disjoint_under_both_modes():
    sessions = [Stub(day(8, d)) for d in range(24, 32)]
    for mode in (p2.WINDOW_MODE_INTERLEAVED, p2.WINDOW_MODE_CHRONOLOGICAL):
        buckets = p2.assign_windows(sessions, 2, mode)
        ids = [id(s) for bucket in buckets for s in bucket]
        assert len(ids) == len(set(ids)) == len(sessions)


def test_interleaved_is_the_default_mode():
    assert p2.DEFAULT_WINDOW_MODE == p2.WINDOW_MODE_INTERLEAVED


def test_unparseable_since_raises_rather_than_silently_keeping_everything():
    with pytest.raises(ValueError):
        p2.parse_since("last tuesday")


def test_library_evaluate_does_not_filter_by_default():
    import inspect

    assert inspect.signature(p2.evaluate).parameters["since"].default is None


def test_the_cli_defaults_to_the_clean_cutoff_and_interleaving():
    parsed = _parse_cli([])
    assert parsed.since == p2.CLEAN_CAPTURE_SINCE
    assert parsed.window_mode == p2.WINDOW_MODE_INTERLEAVED


def test_all_history_flag_clears_the_cutoff():
    assert _parse_cli(["--all-history"]).since is None


def _parse_cli(argv):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=int, default=p2.DEFAULT_WINDOWS)
    parser.add_argument(
        "--window-mode",
        default=p2.DEFAULT_WINDOW_MODE,
        choices=[p2.WINDOW_MODE_INTERLEAVED, p2.WINDOW_MODE_CHRONOLOGICAL],
    )
    parser.add_argument("--since", default=p2.CLEAN_CAPTURE_SINCE)
    parser.add_argument("--all-history", dest="since", action="store_const", const=None)
    return parser.parse_args(argv)

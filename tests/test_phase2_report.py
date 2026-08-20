from __future__ import annotations

from decimal import Decimal

from research.phase2_report import (
    MarketVerdict,
    analyze_session,
    assign_windows,
    build_verdicts,
    evaluate,
    evaluate_site,
    projected_weekly_dollars,
)
from research import adverse_selection as adverse
from research import microstructure as micro
from tests.synthetic_session import SyntheticSession


def busy_session(tmp_path, name, ticker="TEST-A", trades=300, drift="0.0000", site="use1"):
    session = SyntheticSession(str(tmp_path), session_id=name)
    session.market(ticker, vertical="weather", fee_regime="maker_free", maker_multiplier="0")
    session.subscribe([ticker])
    session.snapshot(ticker, [["0.4000", "500.00"]], [["0.5500", "500.00"]])
    for index in range(trades):
        session.advance(1.0).trade(
            ticker, "0.4000", count="10.00", taker_outcome_side="no"
        )
        if drift != "0.0000":
            session.advance(0.5).delta(ticker, "yes", drift, "1.00")
            session.advance(0.5).delta(ticker, "yes", drift, "-1.00")
        else:
            session.advance(1.0).delta(ticker, "yes", "0.3000", "1.00")
            session.advance(1.0).delta(ticker, "yes", "0.3000", "-1.00")
    session.advance(60.0).delta(ticker, "yes", "0.3000", "1.00")
    directory = session.write()
    import json
    import os

    path = os.path.join(directory, "manifest.json")
    with open(path) as handle:
        manifest = json.load(handle)
    manifest["site"] = site
    with open(path, "w") as handle:
        json.dump(manifest, handle)
    return directory


def test_a_market_capturing_spread_with_no_drift_qualifies(tmp_path):
    directory = busy_session(tmp_path, "quiet")
    analysis = analyze_session(directory)

    verdicts = build_verdicts(
        analysis.microstructure.markets,
        analysis.adverse.markets,
        horizon="10s",
        min_quotable_hours=0.0,
    )
    verdict = verdicts[0]

    assert verdict.trades_measured >= 200
    assert verdict.adverse_selection == Decimal("0")
    assert verdict.net_per_contract == Decimal("0.025")
    assert verdict.qualifies
    assert verdict.blockers == []


def test_a_market_with_too_few_trades_is_blocked_not_silently_included(tmp_path):
    directory = busy_session(tmp_path, "thin", trades=10)
    analysis = analyze_session(directory)

    verdict = build_verdicts(
        analysis.microstructure.markets, analysis.adverse.markets, horizon="10s"
    )[0]

    assert not verdict.qualifies
    assert "trades_measured<200" in verdict.blockers
    assert "quotable_hours<24" in verdict.blockers


def test_maker_fees_can_flip_a_market_from_qualifying_to_blocked(tmp_path):
    def analyze(name, maker_multiplier):
        session = SyntheticSession(str(tmp_path), session_id=name)
        session.market(
            "TEST-A",
            fee_regime="maker_free" if maker_multiplier == "0" else "maker_charged",
            maker_multiplier=maker_multiplier,
        )
        session.subscribe(["TEST-A"])
        session.snapshot("TEST-A", [["0.4950", "500.00"]], [["0.4950", "500.00"]])
        for _ in range(300):
            session.advance(1.0).trade(
                "TEST-A", "0.4950", count="10.00", taker_outcome_side="no"
            )
            session.advance(1.0).delta("TEST-A", "yes", "0.3000", "1.00")
            session.advance(1.0).delta("TEST-A", "yes", "0.3000", "-1.00")
        session.advance(60.0).delta("TEST-A", "yes", "0.3000", "1.00")
        analysis = analyze_session(session.write())
        return build_verdicts(
            analysis.microstructure.markets,
            analysis.adverse.markets,
            horizon="10s",
            min_quotable_hours=0.0,
        )[0]

    free = analyze("mm-free", "0")
    charged = analyze("mm-charged", "1")

    assert free.qualifies
    assert not charged.qualifies
    assert "net_per_contract<=0.001" in charged.blockers
    assert charged.net_per_contract < free.net_per_contract


def test_projected_weekly_dollars_scales_with_capture_share():
    verdict = MarketVerdict(
        ticker="TEST-A",
        series_ticker="TEST",
        vertical="weather",
        fee_regime="maker_free",
        quotable_hours=168.0,
        trades_measured=1000,
        contracts_per_week=Decimal(10000),
        edge_median=None,
        net_edge_median=None,
        time_above_breakeven=1.0,
        adverse_selection=Decimal(0),
        net_per_contract=Decimal("0.01"),
        net_lower_bound=Decimal("0.008"),
        qualifies=True,
    )
    assert projected_weekly_dollars([verdict], Decimal("0.05")) == Decimal("4.000")
    assert projected_weekly_dollars([verdict], Decimal("0.5")) == Decimal("40.00")


def test_a_blocked_market_contributes_no_projected_revenue():
    verdict = MarketVerdict(
        ticker="TEST-A",
        series_ticker="TEST",
        vertical="weather",
        fee_regime="maker_free",
        quotable_hours=168.0,
        trades_measured=1,
        contracts_per_week=Decimal(10000),
        edge_median=None,
        net_edge_median=None,
        time_above_breakeven=1.0,
        adverse_selection=Decimal(0),
        net_per_contract=Decimal("0.01"),
        net_lower_bound=Decimal("0.008"),
        qualifies=False,
        blockers=["trades_measured<200"],
    )
    assert projected_weekly_dollars([verdict], Decimal("0.05")) == Decimal(0)


def test_windows_split_the_corpus_by_start_time(tmp_path):
    analyses = [
        analyze_session(busy_session(tmp_path, "early", trades=5)),
        analyze_session(busy_session(tmp_path, "late", trades=5)),
    ]
    analyses[0].started_at_ms = 0
    analyses[1].started_at_ms = 1_000_000

    buckets = assign_windows(analyses, 2)
    assert [len(b) for b in buckets] == [1, 1]


def test_sites_are_evaluated_separately_and_never_merged(tmp_path):
    busy_session(tmp_path, "use1-a", site="use1", trades=5)
    busy_session(tmp_path, "usw2-a", site="usw2", trades=5)

    report = evaluate(str(tmp_path))

    assert set(report["sites"]) == {"use1", "usw2"}
    assert report["sites"]["use1"]["sessions"] == 1
    assert report["sites"]["usw2"]["sessions"] == 1


def test_a_session_captured_with_use_yes_price_is_skipped_with_a_reason(tmp_path):
    session = SyntheticSession(str(tmp_path), session_id="flipped")
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "10.00"]], [["0.5500", "10.00"]])
    session.write(use_yes_price=True)

    report = evaluate(str(tmp_path))
    assert len(report["skipped_sessions"]) == 1
    assert "use_yes_price" in report["skipped_sessions"][0]["reason"]
    assert report["phase2_gate_met"] is False


def test_report_is_json_serializable(tmp_path):
    import json

    busy_session(tmp_path, "one", trades=5)
    report = evaluate(str(tmp_path))
    assert json.loads(json.dumps(report, default=str))["phase2_gate_met"] is False

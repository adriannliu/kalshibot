from __future__ import annotations

import os
import time

import pytest

from research import site_health


@pytest.fixture
def fake_systemctl(monkeypatch):
    table = {}

    def show(unit, properties):
        return table.get(unit, {})

    monkeypatch.setattr(site_health, "_systemctl", show)
    return table


def healthy_units(table):
    table["kalshi-universe.service"] = {"Result": "success", "ExecMainExitTimestamp": "now", "ActiveState": "inactive"}
    table["kalshi-compress.service"] = {"Result": "success", "ExecMainExitTimestamp": "now", "ActiveState": "inactive"}
    table["kalshi-universe.timer"] = {"ActiveState": "active"}
    table["kalshi-compress.timer"] = {"ActiveState": "active"}
    for i in range(4):
        table["kalshi-capture@%d.service" % i] = {
            "ActiveState": "active", "SubState": "running", "NRestarts": "0",
        }


def fresh_universe(root):
    path = os.path.join(root, "universe.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    return path


def test_all_healthy_when_units_pass_and_universe_is_fresh(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    fresh_universe(str(tmp_path))
    units = site_health._units(str(tmp_path), 4)
    assert units["all_healthy"]
    assert not units["universe_stale"]


def test_a_failed_universe_run_is_not_healthy(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    fresh_universe(str(tmp_path))
    fake_systemctl["kalshi-universe.service"]["Result"] = "exit-code"

    units = site_health._units(str(tmp_path), 4)
    assert not units["all_healthy"]
    assert any(not s["healthy"] for s in units["services"])


def test_a_stale_universe_file_is_not_healthy(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    path = fresh_universe(str(tmp_path))
    old = time.time() - (site_health.UNIVERSE_MAX_AGE_HOURS + 5) * 3600
    os.utime(path, (old, old))

    units = site_health._units(str(tmp_path), 4)
    assert units["universe_stale"]
    assert not units["all_healthy"]
    assert units["universe_age_hours"] > site_health.UNIVERSE_MAX_AGE_HOURS


def test_a_missing_universe_file_is_treated_as_stale(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    units = site_health._units(str(tmp_path), 4)
    assert units["universe_stale"]
    assert units["universe_age_hours"] is None
    assert not units["all_healthy"]


def test_a_crash_looping_shard_is_not_healthy(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    fresh_universe(str(tmp_path))
    fake_systemctl["kalshi-capture@1.service"] = {
        "ActiveState": "activating", "SubState": "auto-restart", "NRestarts": "24",
    }

    units = site_health._units(str(tmp_path), 4)
    assert not units["all_healthy"]
    bad = [s for s in units["shards"] if not s["healthy"]]
    assert len(bad) == 1 and bad[0]["restarts"] == 24


def test_an_inactive_timer_is_not_healthy(tmp_path, fake_systemctl):
    healthy_units(fake_systemctl)
    fresh_universe(str(tmp_path))
    fake_systemctl["kalshi-compress.timer"]["ActiveState"] = "inactive"

    units = site_health._units(str(tmp_path), 4)
    assert not units["all_healthy"]


def test_missing_systemd_degrades_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(site_health, "_systemctl", lambda unit, properties: {})
    units = site_health._units(str(tmp_path), 4)
    assert units["services"] == [] and units["timers"] == [] and units["shards"] == []
    assert not units["all_healthy"]

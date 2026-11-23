"""Tests du calcul d'equity reelle et du suivi de portage."""

import pytest

from perps_engine import valuation as V


def quotes(mapping):
    return lambda m: mapping.get(m)


def test_equity_includes_unrealized_pnl_the_api_omits():
    """Le cas reel : l'API annonce 9940, la position vaut +242."""
    pos = [{"market_id": "binance|BTCUSDT", "side": "buy", "size": 1.292,
            "entry_price": 77_165.569}]
    r = V.compute_equity(9_940.15, pos, quotes({"binance|BTCUSDT": 77_352.95}))
    assert r["unrealized_pnl"] == pytest.approx(242.09, abs=0.5)
    assert r["equity"] == pytest.approx(10_182.24, abs=0.5)
    assert r["reliable"]


def test_short_position_pnl_sign():
    pos = [{"market_id": "M", "side": "sell", "size": 2.0, "entry_price": 100.0}]
    r = V.compute_equity(1_000.0, pos, quotes({"M": 90.0}))
    assert r["unrealized_pnl"] == pytest.approx(20.0)


def test_negative_size_is_treated_as_short():
    pos = [{"market_id": "M", "size": -2.0, "entry_price": 100.0}]
    r = V.compute_equity(1_000.0, pos, quotes({"M": 90.0}))
    assert r["unrealized_pnl"] == pytest.approx(20.0)


def test_alternate_field_names_are_tolerated():
    pos = [{"symbol": "M", "direction": "long", "qty": 1.5,
            "average_entry_price": 200.0}]
    r = V.compute_equity(500.0, pos, quotes({"M": 210.0}))
    assert r["unrealized_pnl"] == pytest.approx(15.0)
    assert r["reliable"]


def test_missing_quote_falls_back_to_api_pnl():
    pos = [{"market_id": "M", "side": "buy", "size": 1.0, "entry_price": 100.0,
            "unrealized_pnl": 42.0}]
    r = V.compute_equity(1_000.0, pos, quotes({}))
    assert r["unrealized_pnl"] == pytest.approx(42.0)


def test_unreadable_position_is_flagged_never_silently_zero():
    """Un PnL introuvable ne doit jamais passer pour un PnL nul."""
    pos = [{"weird": "shape"}]
    r = V.compute_equity(1_000.0, pos, quotes({}))
    assert not r["reliable"]
    assert r["incomplete"][0]["raw_keys"] == ["weird"]


def test_no_positions_means_equity_equals_balance():
    r = V.compute_equity(9_940.15, [], quotes({}))
    assert r["equity"] == pytest.approx(9_940.15)
    assert r["reliable"]


def test_funding_tracker_measures_observed_drift():
    """Reproduit le releve : -8.73 $ sur 6h15."""
    f = V.FundingTracker()
    t0 = 1_000_000.0
    f.update(9_948.88, t0)
    out = f.update(9_940.15, t0 + 6.25 * 3600)
    assert out["funding_cumulative"] == pytest.approx(8.73, abs=0.01)
    assert out["funding_per_day"] == pytest.approx(33.5, abs=1.0)


def test_funding_ignores_gains():
    f = V.FundingTracker()
    f.update(10_000.0, 0.0)
    out = f.update(10_500.0, 3_600.0)
    assert out["funding_cumulative"] == 0.0


def test_days_to_floor_countdown():
    assert V.days_to_floor(240.0, 33.5) == pytest.approx(7.2, abs=0.2)
    assert V.days_to_floor(240.0, 0.0) is None


def test_floor_distance_reporting():
    d = V.floor_distance(9_940.15, 9_700.0, 10_000.0)
    assert d["room_usd"] == pytest.approx(240.15)
    assert d["pct_of_drawdown_used"] == pytest.approx(19.95, abs=0.1)

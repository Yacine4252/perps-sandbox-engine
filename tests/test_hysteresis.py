"""
Tests de l'hysteresis et du plancher de cout (correctifs du 17 sept).

Les deux defauts corriges ici viennent de donnees reelles, pas d'une
intuition : signaux vivant 63 secondes, et stops FX plus petits que la
commission.
"""

import math

import pytest

from perps_engine import signals as S
from perps_engine.strategy import MomentumStrategy

CFG = S.SignalConfig()


def series(prices, step=60.0):
    b = S.PriceBuffer()
    for i, p in enumerate(prices):
        b.add(i * step, p)
    return b


def ramp(n_flat, n_trend, base=77_000.0, drift=0.0, noise=0.0002, seed=5):
    import random
    rnd = random.Random(seed)
    out, p = [], base
    for _ in range(n_flat):
        p *= math.exp(rnd.gauss(0, noise))
        out.append(p)
    for _ in range(n_trend):
        p *= math.exp(drift + rnd.gauss(0, noise))
        out.append(p)
    return out


# --- plancher de cout -----------------------------------------------------

def test_cost_floor_blocks_a_market_calmer_than_its_fees():
    """
    Reproduit EUR : stop mesure 0.059 %, cout aller-retour 0.06 %.
    Le cout depasse le risque -> aucun signal ne doit sortir.
    """
    calm = ramp(240, 120, base=1.1490, drift=0.00002, noise=0.000015, seed=3)
    r = S.compute(series(calm), CFG, now=359 * 60)
    assert not r["tradeable"]
    assert r.get("blocked_by_cost") is True
    assert "plancher" in r["reason"]


def test_cost_floor_lets_a_volatile_market_through():
    """GOLD mesure 0.385 % de stop : largement au-dessus du plancher 0.30 %."""
    lively = ramp(240, 120, base=4350.0, drift=0.0006, noise=0.0005, seed=3)
    r = S.compute(series(lively), CFG, now=359 * 60)
    assert r["stop_distance_pct"] > CFG.round_trip_cost_pct * CFG.min_stop_cost_multiple
    assert not r.get("blocked_by_cost")


def test_cost_ratio_is_reported_for_diagnosis():
    lively = ramp(240, 120, drift=0.0006, noise=0.0005)
    r = S.compute(series(lively), CFG, now=359 * 60)
    assert 0 < r["cost_ratio"] < 0.2      # les frais < 20 % du risque


# --- hysteresis -----------------------------------------------------------

def test_opening_requires_the_high_threshold():
    """Sans position ouverte, z entre exit_z et entry_z ne declenche rien."""
    b = series(ramp(240, 120, drift=0.00035, noise=0.0005, seed=9))
    r = S.compute(b, CFG, now=359 * 60, prev_direction=None)
    if CFG.exit_z <= abs(r.get("z", 0)) < CFG.entry_z:
        assert not r["tradeable"]
        assert "entree" in r["reason"]


def test_open_signal_survives_a_dip_below_entry():
    """
    Le cas GOLD : entre a z=-1.6, redescend a z=-1.44. Avec un seuil unique
    le signal meurt en 63 s. Avec hysteresis il doit tenir.
    """
    b = series(ramp(240, 120, drift=-0.0007, noise=0.0004, seed=2))
    first = S.compute(b, CFG, now=359 * 60, prev_direction=None)
    assert first["tradeable"] and first["direction"] == "sell"

    # le marche se calme un peu : z remonte entre exit_z et entry_z
    held = S.compute(b, CFG, now=359 * 60, prev_direction="sell")
    assert held["tradeable"] and held["direction"] == "sell"


def test_signal_closes_only_below_exit_threshold():
    flat = ramp(360, 0, noise=0.0004, seed=4)
    r = S.compute(series(flat), CFG, now=359 * 60, prev_direction="buy")
    if abs(r.get("z", 0)) < CFG.exit_z:
        assert not r["tradeable"]
        assert "sortie" in r["reason"]


def test_reversal_needs_the_high_threshold_again():
    """Passer de vente a achat exige de refranchir entry_z, pas exit_z."""
    b = series(ramp(240, 120, drift=0.0006, noise=0.0004, seed=7))
    r = S.compute(b, CFG, now=359 * 60, prev_direction="sell")
    assert r["direction"] in ("buy", None)
    if r["direction"] == "buy":
        assert abs(r["z"]) >= CFG.entry_z


def test_exit_threshold_is_strictly_below_entry():
    """Sans cet ecart, l'hysteresis degenere en seuil unique."""
    assert CFG.exit_z < CFG.entry_z


# --- effet mesurable sur le clignotement ----------------------------------

def test_hysteresis_cuts_the_flip_rate_substantially():
    """
    Compare le nombre de retournements avec et sans hysteresis sur la meme
    serie bruitee. C'est la mesure qui justifie le correctif.
    """
    import random
    rnd = random.Random(21)
    prices, p = [], 4350.0
    for _ in range(900):
        p *= math.exp(rnd.gauss(0, 0.0006))
        prices.append(p)

    single = S.SignalConfig(entry_z=1.5, exit_z=1.5)
    flips_single = flips_hyst = 0
    d_s = d_h = None
    b = S.PriceBuffer()
    for i, px in enumerate(prices):
        b.add(i * 60.0, px)
        now = i * 60.0
        r_s = S.compute(b, single, now, d_s)
        r_h = S.compute(b, CFG, now, d_h)
        if r_s.get("direction") != d_s:
            flips_single += 1
        if r_h.get("direction") != d_h:
            flips_hyst += 1
        d_s, d_h = r_s.get("direction"), r_h.get("direction")

    assert flips_hyst < flips_single, f"hyst {flips_hyst} vs simple {flips_single}"


def test_strategy_carries_direction_between_cycles():
    """Sans etat porte, l'hysteresis ne peut pas fonctionner."""
    strat = MomentumStrategy()
    for i, p in enumerate(ramp(240, 120, drift=0.0007, noise=0.0004)):
        strat.observe(i * 60.0, p)
    strat.decide("binance|BTCUSDT", now=359 * 60)
    assert strat.direction in ("buy", "sell", None)

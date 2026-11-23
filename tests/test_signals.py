"""Tests du signal momentum normalise par la volatilite."""

import math
import random

import pytest

from perps_engine import signals as S
from perps_engine.strategy import MomentumStrategy


def build(prices, start=0.0, step=60.0):
    b = S.PriceBuffer()
    for i, p in enumerate(prices):
        b.add(start + i * step, p)
    return b


def flat_then_trend(n_flat, n_trend, base=77_000.0, drift=0.0004, noise=0.0002, seed=7):
    rnd = random.Random(seed)
    out, p = [], base
    for _ in range(n_flat):
        p *= math.exp(rnd.gauss(0, noise))
        out.append(p)
    for _ in range(n_trend):
        p *= math.exp(drift + rnd.gauss(0, noise))
        out.append(p)
    return out


CFG = S.SignalConfig()


# --- garde-fous de donnees -----------------------------------------------

def test_no_signal_before_warmup():
    b = build([77_000 + i for i in range(60)])
    r = S.compute(b, CFG, now=59 * 60)
    assert not r["tradeable"] and "chauffe" in r["reason"]


def test_stale_price_blocks_signal():
    b = build(flat_then_trend(200, 100))
    last_ts = 299 * 60
    r = S.compute(b, CFG, now=last_ts + 600)
    assert not r["tradeable"] and r["reason"] == "prix perime"


def test_empty_buffer_is_silent_not_crashing():
    r = S.compute(S.PriceBuffer(), CFG, now=0.0)
    assert not r["tradeable"] and r["reason"] == "aucun prix"


def test_zero_volatility_produces_no_signal():
    b = build([77_000.0] * 400)
    r = S.compute(b, CFG, now=399 * 60)
    assert not r["tradeable"]


# --- comportement du signal ----------------------------------------------

def test_strong_uptrend_triggers_long():
    b = build(flat_then_trend(240, 120, drift=0.0006))
    r = S.compute(b, CFG, now=359 * 60)
    assert r["tradeable"] and r["direction"] == "buy"
    assert r["z"] > CFG.entry_z
    assert r["stop_price"] < r["entry_price"] < r["take_profit_price"]


def test_strong_downtrend_triggers_short():
    b = build(flat_then_trend(240, 120, drift=-0.0006))
    r = S.compute(b, CFG, now=359 * 60)
    assert r["tradeable"] and r["direction"] == "sell"
    assert r["stop_price"] > r["entry_price"] > r["take_profit_price"]


def test_noise_alone_does_not_trigger():
    """Le seuil doit filtrer une marche aleatoire sans derive."""
    fired = 0
    for seed in range(25):
        b = build(flat_then_trend(360, 0, noise=0.0003, seed=seed))
        if S.compute(b, CFG, now=359 * 60)["tradeable"]:
            fired += 1
    assert fired <= 5, f"{fired}/25 declenchements sur du bruit pur"


def test_volatility_normalisation_makes_threshold_regime_independent():
    """
    Meme mouvement relatif, deux regimes de volatilite : le z doit rester
    comparable, sinon le seuil ne veut rien dire d'un jour a l'autre.
    """
    calm = build(flat_then_trend(240, 120, drift=0.0003, noise=0.0001, seed=3))
    wild = build(flat_then_trend(240, 120, drift=0.0003, noise=0.0010, seed=3))
    zc = S.compute(calm, CFG, now=359 * 60)["z"]
    zw = S.compute(wild, CFG, now=359 * 60)["z"]
    assert abs(zc) > abs(zw)   # meme derive, plus de bruit -> moins significatif


def test_stop_distance_scales_with_volatility():
    calm = build(flat_then_trend(240, 120, noise=0.0001, seed=11))
    wild = build(flat_then_trend(240, 120, noise=0.0012, seed=11))
    sc = S.compute(calm, CFG, now=359 * 60)["stop_distance"]
    sw = S.compute(wild, CFG, now=359 * 60)["stop_distance"]
    assert sw > sc * 3


def test_take_profit_respects_r_multiple():
    b = build(flat_then_trend(240, 120, drift=0.0006))
    r = S.compute(b, CFG, now=359 * 60)
    risk = abs(r["entry_price"] - r["stop_price"])
    reward = abs(r["take_profit_price"] - r["entry_price"])
    assert reward / risk == pytest.approx(CFG.tp_r_multiple, abs=0.05)


# --- integration avec le gardien ------------------------------------------

def test_strategy_emits_intent_without_size_or_leverage():
    """L'intention ne doit jamais porter taille ni levier."""
    strat = MomentumStrategy()
    for i, p in enumerate(flat_then_trend(240, 120, drift=0.0006)):
        strat.observe(i * 60.0, p)
    intent = strat.decide("binance|BTCUSDT", now=359 * 60)
    assert intent is not None
    assert intent.size is None and intent.leverage is None
    assert intent.stop_price is not None


def test_intent_passes_the_risk_layer_end_to_end():
    from perps_engine.risk import (AccountRules, AccountState, PlatformLimits,
                                   RiskPolicy, evaluate_order)
    strat = MomentumStrategy()
    for i, p in enumerate(flat_then_trend(240, 120, drift=0.0006)):
        strat.observe(i * 60.0, p)
    intent = strat.decide("binance|BTCUSDT", now=359 * 60)

    rules = AccountRules(10_000, 0.03, 0.03, "static", 0.09)
    st = AccountState(equity=10_011.0, balance=9_940.0)
    v = evaluate_order(intent, st, rules, PlatformLimits(), RiskPolicy())
    assert v.approved, v.reject_codes
    assert v.computed_size > 0 and v.computed_leverage >= 1.0
    assert v.computed["loss_at_stop"] <= v.computed["risk_budget"] + 0.01


def test_carry_hurdle_blocks_when_funding_eats_the_target():
    ok = S.carry_hurdle(800.0, 77_000.0, 15_000.0, funding_per_day=5.0,
                        expected_hold_hours=6)
    bad = S.carry_hurdle(200.0, 77_000.0, 15_000.0, funding_per_day=400.0,
                         expected_hold_hours=24)
    assert ok["carry_ok"] and not bad["carry_ok"]

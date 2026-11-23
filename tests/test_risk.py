"""
Suite de tests du gardien.

Aucune ligne de strategie ni d'execution n'est ecrite tant que ces tests
ne sont pas verts. Ils encodent la discipline : si un test casse plus tard,
c'est la discipline qui a ete relachee, pas un detail technique.
"""

import pytest

from perps_engine.risk import (
    AccountRules, AccountState, OrderIntent, PlatformLimits, RiskPolicy,
    Reject, evaluate_order, liquidation_price, equity_floor,
)


# --- Fixtures generiques : n'importe quel compte, n'importe quelle firme ---

@pytest.fixture
def rules():
    return AccountRules(starting_balance=10_000, daily_loss_pct=0.03,
                        drawdown_pct=0.03, drawdown_type="static",
                        profit_target_pct=0.09)


@pytest.fixture
def limits():
    return PlatformLimits()


@pytest.fixture
def policy():
    return RiskPolicy()


@pytest.fixture
def healthy():
    return AccountState(equity=10_000, balance=10_000)


def intent(**kw):
    base = dict(market_id="binance|BTCUSDT", side="buy", entry_price=77_000.0,
                stop_price=76_500.0, take_profit_price=78_500.0)
    base.update(kw)
    return OrderIntent(**base)


# --- Primitives -----------------------------------------------------------

def test_liquidation_formula_matches_live_platform():
    """Verifie contre un ordre reellement ouvert sur la plateforme."""
    liq = liquidation_price(77_165.569, 10, "buy", 1500)
    assert liq == pytest.approx(70_606.496, abs=0.01)


def test_static_floor_does_not_move_with_profit(rules):
    hot = AccountState(equity=12_000, balance=12_000, high_water_balance=12_000)
    assert equity_floor(rules, hot) == 9_700.0


def test_trailing_floor_follows_high_water_mark():
    r = AccountRules(10_000, 0.03, 0.03, "trailing", 0.09)
    st = AccountState(equity=11_000, balance=11_000, high_water_balance=11_000)
    assert equity_floor(r, st) == pytest.approx(10_670.0)


# --- Rejets structurels ---------------------------------------------------

def test_no_stop_is_rejected(rules, limits, policy, healthy):
    v = evaluate_order(intent(stop_price=None), healthy, rules, limits, policy)
    assert not v.approved and Reject.NO_STOP in v.reject_codes


def test_caller_cannot_impose_size(rules, limits, policy, healthy):
    v = evaluate_order(intent(size=1.0), healthy, rules, limits, policy)
    assert Reject.SIZE_PROVIDED_BY_CALLER in v.reject_codes


def test_caller_cannot_impose_leverage(rules, limits, policy, healthy):
    """Le levier est une consequence arithmetique, pas un curseur d'agressivite."""
    v = evaluate_order(intent(leverage=10.0), healthy, rules, limits, policy)
    assert Reject.LEVERAGE_PROVIDED_BY_CALLER in v.reject_codes


def test_leverage_is_computed_and_keeps_liquidation_past_the_stop(
        rules, limits, policy, healthy):
    v = evaluate_order(intent(), healthy, rules, limits, policy)
    assert v.approved and v.computed_leverage is not None
    liq = v.computed["liquidation_price"]
    assert liq < intent().stop_price          # liquidation SOUS le stop (long)
    assert v.computed["stop_to_liq_buffer_pct"] >= 20.0


def test_stop_on_wrong_side_is_rejected(rules, limits, policy, healthy):
    v = evaluate_order(intent(stop_price=77_500.0), healthy, rules, limits, policy)
    assert Reject.STOP_WRONG_SIDE in v.reject_codes


def test_wide_stop_produces_a_tiny_safe_position_not_a_rejection(
        rules, limits, policy, healthy):
    """
    Contre-intuitif mais correct : un stop tres large (17%) ne rend pas le trade
    dangereux. La taille derivee devient minuscule, le levier tombe a 1x et la
    liquidation reste tres loin sous le stop. Le seul motif de refus ici est le
    ratio risque/rendement, pas le risque de liquidation.

    C'est exactement l'inverse de ce qui a ete fait manuellement sur le compte :
    stop large + taille enorme + levier 10x.
    """
    v = evaluate_order(intent(stop_price=64_000.0), healthy, rules, limits, policy)
    assert v.reject_codes == [Reject.RR_TOO_LOW]
    assert v.computed["chosen_leverage"] == 1.0
    assert v.computed["liquidation_price"] < 64_000.0
    assert v.computed["computed_size"] < 0.01


def test_poor_risk_reward_is_rejected(rules, limits, policy, healthy):
    v = evaluate_order(intent(stop_price=76_500.0, take_profit_price=77_400.0),
                       healthy, rules, limits, policy)
    assert Reject.RR_TOO_LOW in v.reject_codes


# --- Le trade reel qui a declenche tout ca --------------------------------

def test_the_real_oversized_trade_is_rejected(rules, limits, policy):
    """
    Position reellement ouverte : 1.292 BTC, 10x, stop sous la liquidation.
    Doit etre rejetee pour DEUX motifs independants.
    """
    st = AccountState(equity=9_967.60, balance=9_967.60)
    v = evaluate_order(
        intent(entry_price=77_165.569, stop_price=69_363.88, leverage=10.0,
               size=1.292, take_profit_price=77_923.36),
        st, rules, limits, policy)
    assert not v.approved
    assert Reject.SIZE_PROVIDED_BY_CALLER in v.reject_codes
    assert Reject.LEVERAGE_PROVIDED_BY_CALLER in v.reject_codes
    # Le stop de 7801 pts plafonne le levier bien en dessous du 10x utilise.
    assert v.computed["lev_max_for_liquidation"] < 7.0


def test_the_real_trade_size_versus_what_the_engine_would_have_allowed(
        rules, limits, policy):
    """Meme stop, meme entree : quelle taille le moteur aurait-il autorisee ?"""
    st = AccountState(equity=9_967.60, balance=9_967.60)
    v = evaluate_order(intent(entry_price=77_165.569, stop_price=69_363.88,
                              take_profit_price=90_000.0),
                       st, rules, limits, policy)
    assert v.computed["computed_size"] < 0.02      # contre 1.292 reellement pris
    assert 1.292 / v.computed["computed_size"] > 50


# --- Dimensionnement ------------------------------------------------------

def test_valid_intent_is_approved_and_sized(rules, limits, policy, healthy):
    v = evaluate_order(intent(), healthy, rules, limits, policy)
    assert v.approved
    assert v.computed_size > 0
    # La perte au stop, frais compris, ne depasse pas le budget de risque.
    assert v.computed["loss_at_stop"] <= v.computed["risk_budget"] + 0.01


def test_size_is_inversely_proportional_to_stop_distance(rules, limits, policy, healthy):
    tight = evaluate_order(intent(stop_price=76_750.0), healthy, rules, limits, policy)
    wide = evaluate_order(intent(stop_price=76_000.0), healthy, rules, limits, policy)
    assert tight.computed_size > wide.computed_size
    # Meme perte au stop dans les deux cas : c'est tout l'objet de la regle.
    assert tight.computed["loss_at_stop"] == pytest.approx(
        wide.computed["loss_at_stop"], rel=0.05)


def test_fees_are_priced_into_size_not_added_after(rules, limits, policy, healthy):
    cheap = PlatformLimits(taker_fee_bps=1.0)
    dear = PlatformLimits(taker_fee_bps=20.0)
    a = evaluate_order(intent(), healthy, rules, cheap, policy)
    b = evaluate_order(intent(), healthy, rules, dear, policy)
    assert b.computed_size < a.computed_size


def test_risk_shrinks_as_account_approaches_floor(rules, limits, policy):
    """Le test le plus important : le moteur retrecit, il ne s'entete pas."""
    fresh = AccountState(equity=10_000, balance=10_000)
    bruised = AccountState(equity=9_750, balance=9_750)
    a = evaluate_order(intent(), fresh, rules, limits, policy)
    b = evaluate_order(intent(), bruised, rules, limits, policy)
    assert b.computed["risk_budget"] < a.computed["risk_budget"]
    assert b.computed["risk_budget"] == pytest.approx(12.5, abs=0.01)  # 50 * 0.25


def test_impossible_margin_cap_yields_no_valid_leverage(rules, limits, policy, healthy):
    tiny = RiskPolicy(max_margin_utilisation=0.001)
    v = evaluate_order(intent(), healthy, rules, limits, tiny)
    assert Reject.NO_VALID_LEVERAGE in v.reject_codes


# --- Blocages d'etat ------------------------------------------------------

def test_daily_cutoff_blocks_new_trades(rules, limits, policy):
    st = AccountState(equity=9_790, balance=9_790, realized_pnl_today=-210)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.DAILY_CUTOFF_REACHED in v.reject_codes


def test_fourth_trade_in_24h_blocked(rules, limits, policy, healthy):
    st = AccountState(equity=10_000, balance=10_000, trades_last_24h=3)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.TRADE_LIMIT_24H in v.reject_codes


def test_cooldown_after_loss(rules, limits, policy):
    st = AccountState(equity=10_000, balance=10_000, minutes_since_last_loss=20)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.COOLDOWN_ACTIVE in v.reject_codes


def test_below_floor_is_total_stop(rules, limits, policy):
    st = AccountState(equity=9_690, balance=9_690)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.ROOM_EXHAUSTED in v.reject_codes


def test_profit_target_reached_stops_trading(rules, limits, policy):
    st = AccountState(equity=10_950, balance=10_950)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.TARGET_REACHED in v.reject_codes


def test_stale_quote_blocks(rules, limits, policy):
    st = AccountState(equity=10_000, balance=10_000, quote_age_ms=5_000)
    v = evaluate_order(intent(), st, rules, limits, policy)
    assert Reject.STALE_DATA in v.reject_codes


def test_all_reject_reasons_are_returned_not_just_the_first(rules, limits, policy):
    st = AccountState(equity=9_800, balance=9_800, trades_last_24h=5,
                      minutes_since_last_loss=1, quote_age_ms=9_999)
    v = evaluate_order(intent(leverage=20.0, size=2.0), st, rules, limits, policy)
    assert len(v.reject_codes) >= 5


# --- Portabilite entre comptes -------------------------------------------

def test_engine_adapts_to_a_different_account_without_code_change():
    """Compte 100k, drawdown trailing 5%, cible 8% : aucune ligne modifiee."""
    r = AccountRules(100_000, 0.04, 0.05, "trailing", 0.08)
    st = AccountState(equity=104_000, balance=104_000, high_water_balance=104_000)
    v = evaluate_order(intent(), st, r, PlatformLimits(), RiskPolicy())
    assert v.approved
    assert v.computed["floor"] == pytest.approx(98_800.0)
    assert v.computed["risk_budget"] == pytest.approx(1_000.0)  # 1% de 100k


def test_rules_parsed_from_live_api_payload():
    """Charge la reponse reelle de l'API trading-policy."""
    payload = {
        "account_rules": [
            {"id": "max-daily-loss", "parameters": {"pct": 3}},
            {"id": "max-drawdown", "parameters": {"pct": 3, "type": "static"}},
            {"id": "consistency", "parameters": {"pct": 0}},
            {"id": "profit-target", "parameters": {"pct": 9}},
        ],
        "limits": {"min_order_notional_usd": 10, "max_open_positions": 10,
                   "maintenance_margin_ratio_bps": 1500, "max_book_staleness_ms": 2500},
    }
    r = AccountRules.from_policy(payload, starting_balance=10_000)
    lim = PlatformLimits.from_policy(payload)
    assert r.drawdown_type == "static"
    assert r.profit_target_pct == 0.09
    assert r.consistency_pct == 0.0
    assert lim.maintenance_margin_ratio_bps == 1500

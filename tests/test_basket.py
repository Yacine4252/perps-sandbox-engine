"""Tests du panier et du classement transversal."""

import pytest

from perps_engine import basket as B
from perps_engine.signals import SignalConfig


def test_basket_spans_several_asset_classes():
    """Un panier tout-crypto ne diversifie rien : les cryptos bougent ensemble."""
    classes = {m.klass for m in B.DEFAULT_BASKET}
    assert classes == {"crypto", "metal"}
    assert len(B.DEFAULT_BASKET) == 2


def test_crypto_and_metal_have_a_single_representative():
    """
    Deux cryptos ou deux metaux precieux partagent un facteur de risque
    dominant (beta crypto commun ; taux reels pour les metaux) : un seul
    ticker par classe suffit, un second ne fait que payer des frais en
    double sans ajouter d'information.
    """
    by_class = {}
    for m in B.DEFAULT_BASKET:
        by_class.setdefault(m.klass, []).append(m.symbol)
    for k in ("crypto", "metal"):
        assert len(by_class.get(k, [])) == 1, f"{k}: {by_class.get(k)}"


def test_fx_pairs_are_sidelined_on_measured_cost_grounds():
    """
    EUR et JPY ont ete ecartes sur mesure, pas sur opinion : leur distance
    de stop (0.059 % et 0.103 %) ne couvre pas le cout aller-retour de
    0.06 %. Ils restent listes dans INACTIVE_BASKET pour memoire.
    """
    actifs = {m.symbol for m in B.DEFAULT_BASKET}
    assert "EUR" not in actifs and "JPY" not in actifs
    assert {m.symbol for m in B.INACTIVE_BASKET} == {"EUR", "JPY"}


def test_basket_excludes_synthetic_index_products():
    """
    SP500/JP225 ont montre un taux de retournement 2x superieur a BTC sur
    la semaine du 13-15 sept - signe probable d'un artefact de marche
    synthetique illiquide hors heures de cotation. Exclus tant qu'un
    produit equivalent plus liquide n'est pas identifie sur le venue.
    """
    assert "index" not in {m.klass for m in B.DEFAULT_BASKET}
    assert all(m.symbol not in ("SP500", "JP225") for m in B.DEFAULT_BASKET)


def test_basket_market_ids_are_unique():
    ids = [m.market_id for m in B.DEFAULT_BASKET]
    assert len(ids) == len(set(ids))


def test_basket_stays_small_enough_to_mean_something():
    """Garde-fou contre la derive : un panier qui gonfle redevient du bruit."""
    cfg = SignalConfig()
    noise = B.expected_false_positive_rate(len(B.DEFAULT_BASKET), cfg.entry_z)
    assert noise < 2.0, f"{noise} faux signaux attendus par cycle, panier trop large"


def test_false_positive_rate_matches_normal_theory():
    assert B.expected_false_positive_rate(1, 1.96) == pytest.approx(0.05, abs=0.005)
    assert B.expected_false_positive_rate(12, 1.5) == pytest.approx(1.6, abs=0.1)


def test_raising_the_threshold_cuts_the_noise_floor():
    lo = B.expected_false_positive_rate(12, 1.5)
    hi = B.expected_false_positive_rate(12, 2.5)
    assert hi < lo / 5


# --- classement transversal ----------------------------------------------

def m(z=None, tradeable=False):
    return {"z": z, "tradeable": tradeable}


def test_ranking_orders_by_relative_strength():
    r = B.cross_sectional_rank({"BTC": m(2.1), "ETH": m(-0.4), "GOLD": m(0.9)})
    assert r["top"] == "BTC" and r["bottom"] == "ETH"
    assert [x["sym"] for x in r["ranked"]] == ["BTC", "GOLD", "ETH"]


def test_warming_markets_are_absent_not_ranked_last():
    """Un marche en chauffe n'a pas de z : il ne doit pas polluer le classement."""
    r = B.cross_sectional_rank({"BTC": m(2.1), "ETH": m(None), "GOLD": m(0.9)})
    assert [x["sym"] for x in r["ranked"]] == ["BTC", "GOLD"]
    assert r["bottom"] == "GOLD"


def test_breadth_measures_how_many_push_the_same_way():
    r = B.cross_sectional_rank({"A": m(1.0), "B": m(0.5), "C": m(-0.2), "D": m(-1.1)})
    assert r["breadth"] == 0.5


def test_dispersion_separates_a_broad_move_from_a_single_outlier():
    tight = B.cross_sectional_rank({"A": m(0.5), "B": m(0.4), "C": m(0.3)})
    wide = B.cross_sectional_rank({"A": m(2.8), "B": m(0.1), "C": m(-2.2)})
    assert wide["dispersion"] > tight["dispersion"] * 5


def test_ranking_needs_at_least_two_measurable_markets():
    assert B.cross_sectional_rank({"BTC": m(1.0)})["ranked"] == []
    assert B.cross_sectional_rank({})["top"] is None

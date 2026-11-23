"""Tests de la recherche de combinaisons et des veto des sieges."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import combiner as C  # noqa: E402
import rejeu  # noqa: E402
import structure_v1 as S  # noqa: E402
from sieges import CONTRE, NEUTRE, POUR  # noqa: E402
from test_structure import PLAN, CALME, T0, FauxTerrain, faux_marche, lancer  # noqa: E402


def test_masques_et_regles():
    votes = {"tendance_4h": POUR, "structure_h1": CONTRE, "volatilite": NEUTRE, "liquidite": None,
             "of_delta_cvd": POUR, "of_absorption": NEUTRE, "of_funding": CONTRE}
    p, c = C.masques(votes)
    assert p == 1 + 16 + 32          # tendance, delta/CVD, absorption (pas d'objection = pour)
    assert c == 2 + 64               # structure H1, funding
    assert C.garde("veto", 0b1, p, c) and not C.garde("veto", 0b10, p, c)
    assert C.garde("unanime", 0b10001, p, c) and not C.garde("unanime", 0b100, p, c)
    assert C.garde("sans filtre", 0, p, c)
    assert C.nom_config(("veto", 0b1000001)) == "veto : tendance 4 h + funding"


def test_lot_evalue_chaque_combinaison():
    trades = [{"pour": 1, "contre": 0, "r_net": 2.0}, {"pour": 0, "contre": 1, "r_net": -1.0},
              {"pour": 1, "contre": 0, "r_net": 1.0}, {"pour": 0, "contre": 0, "r_net": -0.5}]
    L = C.Lot(trades)
    ev = L.evaluer()
    assert ev[("sans filtre", 0)][0] == 4
    n, m, t = ev[("veto", 1)]                    # garde tout sauf le trade a -1
    assert n == 3 and abs(m - 2.5 / 3) < 1e-12
    sd = math.sqrt(sum((x - m) ** 2 for x in (2.0, 1.0, -0.5)) / 2)
    assert abs(t - m / (sd / math.sqrt(3))) < 1e-9
    assert ev[("unanime", 1)][:2] == (2, 1.5)    # seulement les deux trades ou le siege est pour
    assert L.evaluer(len(trades)) == ev           # decaler d'un tour complet ne change rien
    assert L.evaluer(1)[("unanime", 1)][1] != 1.5


SCENARIOS = [
    ([CALME] * 6 + [(101.0, 101.0, 100.5, 100.6), (100.6, 100.6, 100.3, 100.5),
                    (100.5, 101.5, 100.5, 101.5), (101.5, 102.1, 101.4, 102.0)], 10),
    ([CALME] * 6 + [(101.0, 102.5, 99.8, 100.0), CALME], 8),
    ([CALME] * 6 + [(101.0, 102.2, 100.9, 102.1)] + [(102.1, 102.2, 100.0, 100.1)], 8),
    ([CALME] * (6 + 245) + [(101.0, 101.0, 100.0, 100.1)], 252),
    ([CALME] * 6 + [(101.0, 101.0, 100.3, 100.5)] + [(100.5, 100.6, 100.4, 100.5)] * 1500, 1507),
]


def test_meme_vie_d_ordre_seul_ou_dans_la_boucle():
    for b, n in SCENARIOS:
        dirs = [0] * 5 + [1] * (n - 5)
        boucle, _ = lancer(b, dirs, {5: PLAN})
        seul, _ = S.simuler_independant(FauxTerrain(faux_marche(b, dirs), {5: PLAN}), T0, S.Reglages())
        assert [(x["sortie"], round(x["r_brut"], 9)) for x in boucle] == \
               [(x["sortie"], round(x["r_brut"], 9)) for x in seul]


def test_veto_des_sieges_dans_les_rejeux():
    tout_refuser = lambda *a: True        # noqa: E731
    b = [CALME] * 6 + [(101.0, 101.0, 100.3, 100.5), (100.5, 102.1, 100.5, 102.0)]
    dirs = [0] * 5 + [1] * 3
    tr, ent = S.simuler([FauxTerrain(faux_marche(b, dirs), {5: PLAN})], T0, S.Reglages(), tout_refuser)
    assert tr == [] and ent["veto des sieges"] == 1
    m = faux_marche([CALME] * 12, [0] * 5 + [1, 1, 0, 0, 1, 1, 0])
    tr, refus, _ = rejeu.fidele([m], T0, filtre=tout_refuser)
    assert tr == [] and refus == {"veto des sieges": 2}

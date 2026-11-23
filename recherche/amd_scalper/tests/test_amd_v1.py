"""Tests du portage AMD_Scalper_v1 sur des bougies fabriquees a la main.

Scenario de base (lecture A : compression 2 bougies, ATR 3 bougies) :
  barres 0-30 : range calme 99,5-100,5 ; barre 5 : pic a 102,2 (pool haute a 102,2)
  barre 31    : expansion haussiere 100,3 -> 102,0
  barre 32    : confirmation, plus bas 100,8 > 100,5 -> FVG haussier 100,5 / 100,8
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import amd_v1 as A  # noqa: E402

T0 = 1767571200000  # 2026-01-05 00:00 UTC
EXPANSION = (100.4, 102.0, 100.3, 101.9)
CONFIRMATION = (101.5, 101.7, 100.8, 101.5)
HAUT = (102.2, 102.4, 102.0, 102.2)


def serie(bougies, n_fond=31):
    o, h, l, c = [100.0] * n_fond, [100.5] * n_fond, [99.5] * n_fond, [100.0] * n_fond
    h[5] = 102.2
    for oo, hh, ll, cc in bougies:
        o.append(oo)
        h.append(hh)
        l.append(ll)
        c.append(cc)
    t = [T0 + 300_000 * i for i in range(len(o))]
    return t, o, h, l, c


def lancer(bougies, **kw):
    reglages = dict(frais_bps=0.0, use_ctrl=False, use_pdhl=False)
    reglages.update(kw)
    return A.simuler(*serie(bougies), A.Reglages(**reglages))


def test_conversion_minutes_en_barres():
    assert A.barres(5, 5, 2) == 2
    assert A.barres(14, 5, 2) == 3
    assert A.barres(25, 5, 2) == 5
    assert A.barres(70, 5, 2) == 14
    assert A.barres(30, 5, 1) == 6
    assert A.barres(120, 5, 1) == 24
    assert A.pine_round(2.5) == 3 and A.pine_round(3.5) == 4


def test_rma_comme_pine():
    out = A.rma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0
    assert abs(out[3] - (4 + 2 * 2.0) / 3) < 1e-12
    assert abs(out[4] - (5 + 2 * out[3]) / 3) < 1e-12


def test_pivot_apparait_apres_les_barres_de_droite():
    h = [1, 2, 3, 9, 3, 2, 1, 0, 0]
    ph, _ = A.pivots(h, [5] * 9, 3, 3)
    assert ph[6] == 9
    assert all(v is None for k, v in enumerate(ph) if k != 6)


def test_sessions_new_york_avec_heure_d_ete():
    def ms(*a):
        return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)
    assert A.session(ms(2026, 7, 1, 13, 30)) == 3    # 9 h 30 heure d'ete
    assert A.session(ms(2026, 1, 5, 14, 30)) == 3    # 9 h 30 heure d'hiver
    assert A.session(ms(2026, 1, 5, 14, 25)) == 0    # 9 h 25
    assert A.session(ms(2026, 1, 5, 8, 0)) == 2      # 3 h, Londres
    assert A.session(ms(2026, 1, 5, 23, 30)) == 1    # 18 h 30, Asie


def test_fidele_declenche_sur_la_bougie_de_naissance_realiste_non():
    b = [EXPANSION, CONFIRMATION, (101.5, 102.3, 101.2, 102.2)] + [HAUT] * 8
    fid = lancer(b)
    rea = lancer(b, realiste=True)
    assert fid.stats["entonnoir"]["fvg"] == 1
    assert [x["sortie"] for x in fid.trades] == ["tp"]
    x = fid.trades[0]
    assert x["entree"] == 100.8 and 2.0 <= x["rr"] <= 4.0
    assert abs(x["r_net"] - x["rr"]) < 1e-12
    assert rea.stats["n_trades"] == 0          # le prix n'est jamais revenu sur le FVG


def test_retour_sur_le_fvg_gagne_dans_les_deux_modes():
    b = [EXPANSION, CONFIRMATION, (101.5, 101.9, 101.0, 101.2), (101.2, 101.5, 100.7, 101.3),
         (101.3, 102.3, 101.2, 102.2)] + [HAUT] * 3
    fid, rea = lancer(b), lancer(b, realiste=True)
    assert [x["sortie"] for x in fid.trades] == ["tp"]
    assert [x["sortie"] for x in rea.trades] == ["tp"]
    assert rea.trades[0]["t_ouverture"] == T0 + 300_000 * 34     # rempli au retour, pas a la naissance


def test_stop_dans_la_bougie_de_remplissage_en_realiste():
    b = [EXPANSION, CONFIRMATION, (101.5, 101.9, 101.0, 101.2), (101.2, 101.5, 100.3, 100.6)] \
        + [(100.6, 100.9, 100.4, 100.6)] * 3
    rea = lancer(b, realiste=True)
    assert [x["sortie"] for x in rea.trades] == ["sl"]
    assert abs(rea.trades[0]["r_net"] + 1.0) < 1e-12


def test_bougie_qui_touche_stop_et_objectif_compte_une_perte():
    b = [EXPANSION, CONFIRMATION, (101.5, 102.3, 100.3, 101.0)] + [(101.0, 101.2, 100.8, 101.0)] * 3
    assert [x["sortie"] for x in lancer(b).trades] == ["sl"]


def test_time_stop_zero_r_en_fidele_prix_de_cloture_en_realiste():
    b = [EXPANSION, CONFIRMATION, (101.5, 101.6, 100.75, 101.2)] + [(101.0, 101.3, 100.9, 101.2)] * 30
    fid, rea = lancer(b), lancer(b, realiste=True)
    assert [x["sortie"] for x in fid.trades] == ["ts"] and fid.trades[0]["r_net"] == 0.0
    x = rea.trades[0]
    assert x["sortie"] == "ts"
    assert abs(x["r_net"] - (101.2 - 100.8) / x["risque"]) < 1e-12


def test_frais_en_r():
    b = [EXPANSION, CONFIRMATION, (101.5, 102.3, 101.2, 102.2)] + [HAUT] * 8
    x = lancer(b, frais_bps=3.0).trades[0]
    attendu = 3e-4 * (x["entree"] + x["prix_sortie"]) / x["risque"]
    assert abs((x["r_brut"] - x["r_net"]) - attendu) < 1e-12

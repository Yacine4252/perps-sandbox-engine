"""Tests des trois gestions du stop de gestion_or.py, sur un marche fabrique."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gestion_or as G  # noqa: E402
from test_sorties_or import T0, marche  # noqa: E402

ENTREE = [(100, 100, 100, 100, 0), (100, 100, 100, 100, 1)]       # achat a 100, stop 99, objectif 102
PLUS_UN_R = (100, 101.2, 100, 101, 1)                                # +1 R touche


def un_trade(barres, regle, fidele=False):
    m = marche(barres)
    tr = G.fidele(m, T0, regle)[0] if fidele else G.tous(m, T0, regle)
    assert len(tr) == 1
    return tr[0]


def test_partiel_moitie_a_plus_un_r_puis_stop():
    barres = ENTREE + [PLUS_UN_R, (101, 101, 98.5, 98.8, 1)]
    x = un_trade(barres, "partiel")
    assert x["sortie"] == "+1R+sl" and x["r_brut"] == 0.0
    assert un_trade(barres, "tenir")["r_brut"] == -1.0


def test_partiel_moitie_a_plus_un_r_puis_objectif():
    x = un_trade(ENTREE + [PLUS_UN_R, (101, 102.5, 100.9, 102.2, 1)], "partiel")
    assert x["sortie"] == "+1R+tp" and x["r_brut"] == 1.5


def test_suiveur_remonte_le_stop_a_plus_un_demi_r():
    barres = ENTREE + [PLUS_UN_R, (101, 101, 100.4, 100.6, 1), (100.6, 100.7, 100.5, 100.6, 0)]
    x = un_trade(barres, "suiveur")
    assert x["sortie"] == "stop suiveur" and x["r_brut"] == 0.5
    assert un_trade(barres, "tenir")["sortie"] == "fin"


def test_stop_d_abord_si_tout_dans_la_meme_minute():
    x = un_trade(ENTREE + [(100, 101.5, 98.9, 100, 1)], "partiel")
    assert x["sortie"] == "sl" and x["r_brut"] == -1.0


def test_sortie_au_temps_sans_plus_un_r():
    calme = [(100.2, 100.5, 99.8, 100.2, 1)] * 300
    x = un_trade(ENTREE + calme, "temps4h")
    assert x["sortie"] == "temps" and round(x["r_brut"], 6) == 0.2
    assert round(x["duree_min"]) == 240
    assert un_trade(ENTREE + calme, "temps8h")["sortie"] == "fin"     # 300 min < 8 h


def test_pas_de_sortie_au_temps_apres_plus_un_r():
    barres = ENTREE + [PLUS_UN_R] + [(100.5, 100.8, 100.2, 100.5, 1)] * 300
    assert un_trade(barres, "temps4h")["sortie"] == "fin"


def test_fidele_et_tous_d_accord_pour_un_seul_signal():
    barres = ENTREE + [PLUS_UN_R, (101, 101, 100.4, 100.6, 1), (100.6, 102.5, 100.5, 102.2, 0)]
    for regle in G.REGLES:
        assert un_trade(barres, regle, fidele=True)["r_brut"] == un_trade(barres, regle)["r_brut"]

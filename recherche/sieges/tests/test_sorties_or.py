"""Tests des trois sorties comparees par sorties_or.py, sur un marche fabrique."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rejeu  # noqa: E402
import sorties_or as SO  # noqa: E402

T0 = 1_780_000_000_000


def marche(barres):
    """barres : (o, h, l, c, dir). Signal vu, distance du stop 1, sans frais."""
    n = len(barres)
    return rejeu.Marche("GOLD", [T0 + k * 60_000 for k in range(n)],
                        [b[0] for b in barres], [b[1] for b in barres],
                        [b[2] for b in barres], [b[3] for b in barres],
                        [b[4] for b in barres], [1.0] * n, [2.0] * n, [True] * n, 0.0)


DEBUT = [(100, 100, 100, 100, 0), (100, 100, 100, 100, 1),        # achat a 100, stop 99, objectif 102
         (100, 101.2, 100, 101, 1)]                                 # le trade gagne 1 R
GAGNANT_PUIS_OBJECTIF = DEBUT + [(101, 101.1, 100.8, 101, 0),      # le signal retombe
                                 (101, 102.5, 100.9, 102.2, 0)]    # l'objectif est touche
GAGNANT_PUIS_RETOUR = DEBUT + [(101, 101.1, 100.8, 101, 0),
                               (101, 101, 99.9, 100, 0)]           # retour au prix d'entree
PERDANT = [(100, 100, 100, 100, 0), (100, 100, 100, 100, 1),
           (100, 100, 99.4, 99.5, 1), (99.5, 99.6, 99.4, 99.5, 0),  # le signal retombe en perte
           (99.5, 99.5, 98.5, 98.8, 0)]                             # puis le stop


def un_trade(barres, mode, fidele=False):
    m = marche(barres)
    tr = SO.fidele_or(m, T0, mode)[0] if fidele else SO.tous_or(m, T0, mode)
    assert len(tr) == 1
    return tr[0]


def test_gagnant_rapide_sort_au_signal_tenir_et_hybride_vont_a_l_objectif():
    assert un_trade(GAGNANT_PUIS_OBJECTIF, "rapide")["r_brut"] == 1.0
    for mode in ("tenir", "hybride"):
        x = un_trade(GAGNANT_PUIS_OBJECTIF, mode)
        assert x["sortie"] == "tp" and x["r_brut"] == 2.0


def test_hybride_remonte_le_stop_au_prix_d_entree():
    x = un_trade(GAGNANT_PUIS_RETOUR, "hybride")
    assert x["sortie"] == "stop a l'entree" and x["r_brut"] == 0.0
    assert un_trade(GAGNANT_PUIS_RETOUR, "tenir")["sortie"] == "fin"   # stop 99 jamais touche


def test_perdant_coupe_au_signal_sauf_en_tenir():
    for mode in ("rapide", "hybride"):
        x = un_trade(PERDANT, mode)
        assert x["sortie"] == "signal" and x["r_brut"] == -0.5
    x = un_trade(PERDANT, "tenir")
    assert x["sortie"] == "sl" and x["r_brut"] == -1.0


def test_rejeu_fidele_donne_la_meme_chose_pour_un_seul_signal():
    for barres in (GAGNANT_PUIS_OBJECTIF, GAGNANT_PUIS_RETOUR, PERDANT):
        for mode in SO.MODES:
            assert un_trade(barres, mode, fidele=True)["r_brut"] == un_trade(barres, mode)["r_brut"]

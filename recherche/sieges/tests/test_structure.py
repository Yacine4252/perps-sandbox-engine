"""Tests de la table structurelle v1 sur des bougies fabriquees a la main."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rejeu  # noqa: E402
import structure_v1 as S  # noqa: E402
from donnees import MIN_MS, Bougies  # noqa: E402

T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)   # lundi 00:00 UTC


def ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def test_sessions_heure_de_new_york():
    assert S.session(ms(2026, 1, 5, 14, 0)) == "New York"      # 9 h
    assert S.session(ms(2026, 1, 5, 8, 0)) == "Londres"        # 3 h
    assert S.session(ms(2026, 1, 5, 1, 0)) == "Asie"           # dimanche 20 h
    assert S.session(ms(2026, 1, 5, 22, 30)) == "hors session"  # 17 h 30
    assert S.session(ms(2026, 1, 10, 15, 0)) == "week-end"      # samedi
    assert S.session(ms(2026, 1, 9, 22, 30)) == "week-end"      # vendredi 17 h 30
    assert S.session(ms(2026, 7, 6, 13, 0)) == "New York"       # 9 h en heure d'ete


def test_order_block_achat_stop_sous_le_dernier_plus_bas():
    #        ouverture haut   bas   cloture
    b5 = [(100.0, 100.5, 99.8, 100.2),
          (100.2, 100.3, 99.5, 99.6),      # derniere bougie baissiere : l'OB
          (99.6, 99.9, 99.2, 99.8),        # le plus bas du mouvement (bougie haussiere)
          (99.8, 100.9, 99.7, 100.8),
          (100.8, 101.6, 100.7, 101.5)]
    m5 = Bougies([k * 300_000 for k in range(5)], *[list(x) for x in zip(*b5)])
    entree, ext, t_mvt = S.trouver_ob(m5, 4, 1, 5, 6)
    assert entree == 100.2 and ext == 99.2 and t_mvt == 2 * 300_000


def test_order_block_vente():
    b5 = [(100.0, 100.2, 99.5, 99.8),
          (99.8, 100.6, 99.7, 100.5),      # derniere bougie haussiere : l'OB
          (100.5, 100.9, 100.1, 100.2),    # le plus haut du mouvement
          (100.2, 100.3, 99.1, 99.2)]
    m5 = Bougies([k * 300_000 for k in range(4)], *[list(x) for x in zip(*b5)])
    entree, ext, _ = S.trouver_ob(m5, 3, -1, 4, 6)
    assert entree == 99.8 and ext == 100.9


def test_cassures_de_structure_15_min():
    #      0    1    2    3    4    5    6    7    8    9   10   11   12
    h = [100, 101, 102, 105, 102, 101, 100, 101, 103, 106, 104, 103, 102]
    l = [98, 99, 100, 103, 100, 99, 97, 99, 101, 104, 102, 101, 96]
    c = [99, 100, 101, 104, 101, 100, 98, 100, 102, 105.5, 103, 102, 96.5]
    b = Bougies(list(range(13)), c, h, l, c)
    # sommet 105 (bougie 3) confirme a la bougie 6, casse par la cloture 105,5 (bougie 9) ;
    # creux 97 (bougie 6) confirme a la bougie 9, casse par la cloture 96,5 (bougie 12)
    assert S.cassures(b, 3) == [(9, 1), (12, -1)]


def test_cible_premier_niveau_a_3r_ou_plus():
    niv = [("a", 101.0), ("b", 103.0), ("c", 106.0)]
    assert S.choisir_cible(niv, 100.0, 1.0, 1, 3.0) == ("b", 103.0)
    assert S.choisir_cible(niv, 100.0, 1.0, 1, 4.0) == ("c", 106.0)
    assert S.choisir_cible(niv, 100.0, 1.0, 1, 7.0) is None
    assert S.choisir_cible([("x", 97.0), ("y", 94.0)], 100.0, 1.0, -1, 3.0) == ("x", 97.0)


# --------------------------------------------------------------------------
# cycle de vie d'un ordre, avec un terrain dont le plan est impose
# --------------------------------------------------------------------------

class FauxTerrain:
    def __init__(self, m, plans):
        self.m, self.plans = m, plans
        self.evenements = {i: m.dir[i] for i in range(1, len(m.t))
                           if m.dir[i] != m.dir[i - 1] and m.dir[i] != 0 and m.vu[i]}

    def preparer(self, i, d):
        p = self.plans.get(i)
        if p is None:
            return None, "aucun plan"
        return dict({"sym": self.m.sym, "d": d, "cible": "test", "ny": False}, i_signal=i,
                    t_signal=self.m.t[i] + MIN_MS, rr=(p["objectif"] - p["entree"]) / (p["entree"] - p["stop"]),
                    **p), "ok"


def faux_marche(bougies, dirs):
    o, h, l, c = (list(x) for x in zip(*bougies))
    n = len(c)
    return rejeu.Marche("BTC", [T0 + MIN_MS * k for k in range(n)], o, h, l, c, dirs,
                        [1.0] * n, [2.5] * n, [True] * n, 0.0)


CALME = (101.0, 101.1, 100.9, 101.0)
PLAN = {"entree": 100.4, "stop": 99.9, "objectif": 102.0}


def lancer(bougies, dirs, plans):
    return S.simuler([FauxTerrain(faux_marche(bougies, dirs), plans)], T0, S.Reglages())


def test_ordre_rempli_au_retour_puis_objectif():
    b = [CALME] * 6 + [(101.0, 101.0, 100.5, 100.6), (100.6, 100.6, 100.3, 100.5),
                       (100.5, 101.5, 100.5, 101.5), (101.5, 102.1, 101.4, 102.0)]
    dirs = [0] * 5 + [1] * 5
    tr, ent = lancer(b, dirs, {5: PLAN})
    assert ent["rempli"] == 1 and [x["sortie"] for x in tr] == ["tp"]
    assert tr[0]["entree"] == 100.4 and abs(tr[0]["r_brut"] - 3.2) < 1e-9


def test_stop_dans_la_minute_du_remplissage_compte_une_perte():
    b = [CALME] * 6 + [(101.0, 102.5, 99.8, 100.0), CALME]   # touche entree, stop ET objectif
    tr, _ = lancer(b, [0] * 5 + [1] * 3, {5: PLAN})
    assert [x["sortie"] for x in tr] == ["sl"] and abs(tr[0]["r_brut"] + 1.0) < 1e-9


def test_ordre_annule_si_l_objectif_est_atteint_sans_retour():
    b = [CALME] * 6 + [(101.0, 102.2, 100.9, 102.1)] + [(102.1, 102.2, 100.0, 100.1)]
    tr, ent = lancer(b, [0] * 5 + [1] * 3, {5: PLAN})
    assert tr == [] and ent["objectif atteint sans retour"] == 1


def test_ordre_expire_apres_4_heures():
    b = [CALME] * (6 + 245) + [(101.0, 101.0, 100.0, 100.1)]
    tr, ent = lancer(b, [0] * 5 + [1] * (len(b) - 5), {5: PLAN})
    assert tr == [] and ent["ordre expire (4 h)"] == 1


def test_position_fermee_apres_24_heures():
    b = [CALME] * 6 + [(101.0, 101.0, 100.3, 100.5)] + [(100.5, 100.6, 100.4, 100.5)] * 1500
    tr, _ = lancer(b, [0] * 5 + [1] * (len(b) - 5), {5: PLAN})
    assert [x["sortie"] for x in tr] == ["temps"] and 1439 <= tr[0]["duree_min"] <= 1441


def _bloc_gagnant():
    return [CALME, (101.0, 101.0, 100.3, 100.5), (100.5, 102.1, 100.5, 102.0)] + [CALME] * 97


def test_une_place_gardee_pour_new_york():
    b = [CALME] * 5 + _bloc_gagnant() * 4
    dirs = [0] * 5 + ([1, 1, 0] + [0] * 97) * 4
    plans = {5: PLAN, 105: PLAN, 205: PLAN, 305: dict(PLAN, ny=True)}
    tr, ent = lancer(b, dirs, plans)
    assert ent["refus : place gardee pour New York"] == 1
    assert len(tr) == 3 and all(x["sortie"] == "tp" for x in tr)
    assert tr[-1]["ny"] is True


def test_pause_apres_une_perte():
    perte = [CALME, (101.0, 101.0, 99.8, 100.0)] + [CALME] * 8
    b = [CALME] * 5 + perte + _bloc_gagnant()
    dirs = [0] * 5 + [1, 1] + [0] * 8 + [1, 1, 0] + [0] * 97
    tr, ent = lancer(b, dirs, {5: PLAN, 15: PLAN})
    assert [x["sortie"] for x in tr] == ["sl"] and ent["refus : pause apres une perte"] == 1

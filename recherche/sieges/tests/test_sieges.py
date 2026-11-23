"""Tests du rejeu et des sieges sur des bougies fabriquees a la main.
A lancer avec le python du venv du moteur (le rejeu importe sandbox_run)."""
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyse  # noqa: E402
import rejeu  # noqa: E402
from donnees import (HEURE_MS, JOUR_MS, MIN_MS, Bougies, agreger,  # noqa: E402
                     decalage_new_york, derniere_fermee)
from sieges import CONTRE, NEUTRE, POUR, Contexte, ema, structure  # noqa: E402

T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)   # lundi 00:00 UTC


def ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def marche(sym, closes, dirs, dist=1.0, vu=None, hauts=None, bas=None, frais=0.0):
    n = len(closes)
    o = [closes[0]] + closes[:-1]
    h = hauts or [max(o[k], closes[k]) for k in range(n)]
    l = bas or [min(o[k], closes[k]) for k in range(n)]
    return rejeu.Marche(sym, [T0 + MIN_MS * k for k in range(n)], o, h, l, list(closes), list(dirs),
                        [dist if d else None for d in dirs], [2.5 if d else None for d in dirs],
                        vu or [True] * n, frais)


def bougies(closes, v=1.0, tb=0.5, hauts=None, bas=None, t0=T0):
    n = len(closes)
    o = [closes[0]] + closes[:-1]
    h = hauts or [max(o[k], closes[k]) for k in range(n)]
    l = bas or [min(o[k], closes[k]) for k in range(n)]
    vv = v if isinstance(v, list) else [v] * n
    tt = tb if isinstance(tb, list) else [tb] * n
    return Bougies([t0 + MIN_MS * k for k in range(n)], o, h, l, list(closes), vv, tt)


# --------------------------------------------------------------------------
# rejeu
# --------------------------------------------------------------------------

def test_objectif_a_2r():
    m = marche("BTC", [100] * 6 + [100.5, 102.5, 102.5], [0] * 5 + [1, 1, 1, 1])
    tr = rejeu.tous_les_signaux(m, T0)
    assert [x["sortie"] for x in tr] == ["tp", ]
    x = tr[0]
    assert x["entree"] == 100 and x["stop"] == 99 and x["objectif"] == 102
    assert abs(x["r_brut"] - 2.0) < 1e-12


def test_sortie_quand_le_signal_retombe():
    m = marche("BTC", [100] * 6 + [100.6, 100.8, 100.8], [0] * 5 + [1, 1, 0, 0])
    tr = rejeu.tous_les_signaux(m, T0)
    assert [x["sortie"] for x in tr] == ["signal"]
    assert abs(tr[0]["r_brut"] - 0.8) < 1e-12 and tr[0]["prix_sortie"] == 100.8


def test_stop_et_objectif_dans_la_meme_bougie_compte_le_stop():
    closes = [100] * 6 + [100.5, 100.5]
    h = [100] * 6 + [102.5, 100.5]
    l = [100] * 6 + [98.5, 100.5]
    m = marche("BTC", closes, [0] * 5 + [1, 1, 1], hauts=h, bas=l)
    x = rejeu.tous_les_signaux(m, T0)[0]
    assert x["sortie"] == "sl" and x["ambigu"] and abs(x["r_brut"] + 1.0) < 1e-12


def test_ouverture_au_dela_du_stop_sort_a_l_ouverture():
    closes = [100] * 6 + [98.0, 98.0]
    m = marche("BTC", closes, [0] * 5 + [1, 1, 1])
    m.o[6], m.h[6], m.l[6] = 98.5, 98.6, 97.9
    x = rejeu.tous_les_signaux(m, T0)[0]
    assert x["sortie"] == "sl" and x["prix_sortie"] == 98.5


def test_signal_ne_pendant_la_chauffe_ignore():
    vu = [True] * 9
    vu[5] = False
    m = marche("BTC", [100] * 9, [0] * 5 + [1, 1, 0, 0], vu=vu)
    assert rejeu.tous_les_signaux(m, T0) == []


def test_renversement_ferme_puis_ouvre_en_face():
    m = marche("BTC", [100] * 6 + [100.2, 100.3, 100.3], [0] * 5 + [1, 1, -1, -1])
    tr = rejeu.tous_les_signaux(m, T0)
    assert [x["sortie"] for x in tr] == ["signal", "fin"]
    assert tr[0]["sens"] == 1 and tr[1]["sens"] == -1 and tr[1]["entree"] == 100.3
    assert tr[1]["stop"] == 101.3 and tr[1]["objectif"] == 98.3


def test_frais_en_r():
    m = marche("GOLD", [100] * 6 + [100.6, 100.8, 100.8], [0] * 5 + [1, 1, 0, 0], frais=0.5)
    x = rejeu.tous_les_signaux(m, T0)[0]
    assert abs(x["frais_r"] - 0.5e-4 * (100 + 100.8) / 1.0) < 1e-12
    assert abs(x["r_net"] - (x["r_brut"] - x["frais_r"])) < 1e-12


def test_avant_v24_le_signal_qui_retombe_ne_ferme_pas():
    closes = [100] * 6 + [100.6, 100.8, 101.5, 102.5, 102.5]
    dirs = [0] * 5 + [1, 1, 0, 0, 0, 0]
    m = marche("BTC", closes, dirs)
    assert [x["sortie"] for x in rejeu.tous_les_signaux(m, T0)] == ["signal"]
    tr = rejeu.tous_les_signaux(m, T0, sortie_signal=False)
    assert [x["sortie"] for x in tr] == ["tp"] and abs(tr[0]["r_brut"] - 2.0) < 1e-12


def test_avant_v24_les_trades_d_un_marche_se_chevauchent():
    closes = [100] * 6 + [100.2] * 5
    dirs = [0] * 5 + [1, 0, 1, 1, 0, 0]
    tr = rejeu.tous_les_signaux(marche("BTC", closes, dirs), T0, sortie_signal=False)
    assert len(tr) == 2 and all(x["sortie"] == "fin" for x in tr)
    assert tr[0]["t_entree"] < tr[1]["t_entree"]


def test_fidele_avant_v24_la_position_bloque_les_signaux_suivants():
    closes = [100] * 6 + [100.2] * 10
    dirs = [0] * 5 + [1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0]
    m = marche("BTC", closes, dirs)
    tr, refus, _ = rejeu.fidele([m], T0, sortie_signal=False)
    assert len(tr) == 1 and tr[0]["sortie"] == "fin"
    assert refus == {"POSITION_LIMIT_REACHED": 1}
    tr2, refus2, _ = rejeu.fidele([m], T0)
    assert [x["sortie"] for x in tr2] == ["signal", "signal"] and not refus2


def test_fidele_une_position_a_la_fois():
    btc = marche("BTC", [100] * 30, [0] * 5 + [1] * 25)
    gold = marche("GOLD", [100] * 30, [0] * 7 + [1] * 23)
    tr, refus, tentatives = rejeu.fidele([btc, gold], T0)
    assert [x["sym"] for x in tr] == ["BTC"]
    assert refus == {"POSITION_LIMIT_REACHED": 1} and tentatives == 2


def _signaux_gagnants(n_signaux, ecart_min=60, gain=0.5):
    """n signaux d'achat, chacun ferme sur un gain de `gain` apres 2 minutes."""
    closes, dirs = [100.0] * 5, [0] * 5
    p = 100.0
    for _ in range(n_signaux):
        closes += [p, p + gain / 2, p + gain] + [p + gain] * (ecart_min - 3)
        dirs += [1, 1, 0] + [0] * (ecart_min - 3)
        p += gain
    return closes, dirs


def test_fidele_trois_trades_par_24h():
    closes, dirs = _signaux_gagnants(4)
    tr, refus, _ = rejeu.fidele([marche("BTC", closes, dirs)], T0)
    assert len(tr) == 3 and all(x["r_net"] > 0 for x in tr)
    assert refus == {"TRADE_LIMIT_24H": 1}


def test_fidele_pause_de_45_min_apres_une_perte():
    # perte a la minute 7, nouveau signal 10 min plus tard (refuse), puis 50 min plus tard (pris)
    closes = [100.0] * 5 + [100, 99.8, 99.6] + [99.6] * 9 + [99.6, 99.6, 99.6] + [99.6] * 38 + [99.6] * 10
    dirs = [0] * 5 + [1, 1, 0] + [0] * 9 + [1, 1, 0] + [0] * 38 + [1] * 10
    tr, refus, _ = rejeu.fidele([marche("BTC", closes, dirs)], T0)
    assert refus == {"COOLDOWN_ACTIVE": 1}
    assert len(tr) == 2 and tr[0]["r_net"] < 0
    assert (tr[1]["t_entree"] - tr[0]["t_sortie"]) / MIN_MS >= 45


# --------------------------------------------------------------------------
# outils de temps et indicateurs
# --------------------------------------------------------------------------

def test_heure_de_new_york():
    assert decalage_new_york(ms(2026, 3, 8, 7, 0)) == -4 * HEURE_MS
    assert decalage_new_york(ms(2026, 3, 8, 6, 59)) == -5 * HEURE_MS
    assert decalage_new_york(ms(2026, 11, 1, 6, 0)) == -5 * HEURE_MS
    assert decalage_new_york(ms(2026, 11, 1, 5, 59)) == -4 * HEURE_MS


def test_bougie_4h_utilisee_seulement_fermee():
    b = bougies([100.0] * (3 * 240))
    h4 = agreger(b, 4 * HEURE_MS)
    assert len(h4.t) == 3
    assert derniere_fermee(h4, 4 * HEURE_MS, T0 + 8 * HEURE_MS) == 1
    assert derniere_fermee(h4, 4 * HEURE_MS, T0 + 8 * HEURE_MS - MIN_MS) == 0
    assert derniere_fermee(h4, 4 * HEURE_MS, T0 + 4 * HEURE_MS - MIN_MS) == -1


def test_ema_amorcee_par_moyenne():
    e = ema([1.0, 2.0, 3.0, 4.0], 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and abs(e[3] - (0.5 * 4 + 0.5 * 2.0)) < 1e-12


def test_structure_choch_haussier_puis_baissier():
    #            0    1    2    3    4    5    6    7    8    9   10   11   12   13   14   15
    hauts = [100, 101, 102, 105, 102, 101, 100, 104, 106, 107, 104, 103, 102, 101, 100, 99]
    bas = [98, 99, 100, 103, 100, 99, 97, 100, 104, 105, 102, 101, 100, 99, 98, 95]
    clos = [99, 100, 101, 104, 101, 100, 98, 103, 105.5, 106, 103, 102, 101, 100, 99, 96]
    b = Bougies(list(range(16)), clos, hauts, bas, clos)
    etat, genre = structure(b, 3)
    assert etat[7] == 0                    # le sommet de la bougie 3 (105) n'est pas casse
    assert etat[8] == 1 and genre[8] == "CHoCH"    # cloture 105,5 > 105
    assert etat[15] == -1 and genre[15] == "CHoCH"  # cloture 96 < creux de la bougie 6 (97)


# --------------------------------------------------------------------------
# sieges
# --------------------------------------------------------------------------

def test_tendance_4h():
    n = 60 * 4 * 60                       # 60 bougies 4 h en hausse reguliere
    closes = [100 + 0.001 * k for k in range(n)]
    ctx = Contexte(bougies(closes))
    i = n - 1
    assert ctx.voter(i, 1, closes[i], closes[i] + 1)[0]["tendance_4h"] == POUR
    assert ctx.voter(i, -1, closes[i], closes[i] - 1)[0]["tendance_4h"] == CONTRE
    assert ctx.voter(60 * 4 * 40, 1, 100, 101)[0]["tendance_4h"] is None   # moins de 50 bougies 4 h


def test_volatilite_en_rang_centile():
    jours = 9
    n = jours * 1440
    calme = [100 + (0.01 if k % 2 else -0.01) for k in range(n)]
    agite = [100 + (0.5 if k % 2 else -0.5) for k in range(n)]
    fin_agitee = calme[:8 * 1440] + agite[8 * 1440:]
    fin_calme = agite[:8 * 1440] + calme[8 * 1440:]
    i = n - 1
    assert Contexte(bougies(fin_agitee)).voter(i, 1, 100, 101)[0]["volatilite"] == POUR
    assert Contexte(bougies(fin_calme)).voter(i, 1, 100, 101)[0]["volatilite"] == CONTRE


def test_liquidite_pdh_et_niveau_deja_pris():
    # jour 1 : plus haut 105 a 12:00 ; jour 2 : signal a 10:00 UTC
    n = 1440 + 10 * 60
    closes = [100.0] * n
    hauts = [100.0] * n
    hauts[12 * 60] = 105.0
    ctx = Contexte(bougies(closes, hauts=hauts))
    i = n - 1
    v, info = ctx.voter(i, 1, 100.0, 106.0)
    assert v["liquidite"] == CONTRE and "PDH" in info["obstacles"]
    assert ctx.voter(i, 1, 100.0, 104.0)[0]["liquidite"] == POUR
    # le meme jour 2, a 08:00 UTC, le prix est deja monte a 105,5 : PDH pris
    hauts_pris = list(hauts)
    hauts_pris[1440 + 8 * 60] = 105.5
    ctx3 = Contexte(bougies(closes, hauts=hauts_pris))
    assert ctx3.voter(i, 1, 100.0, 106.0)[0]["liquidite"] == POUR


def test_delta_cvd():
    n = 200
    acheteur = Contexte(bougies([100.0] * n, v=1.0, tb=0.8))
    assert acheteur.voter(n - 1, 1, 100, 101)[0]["of_delta_cvd"] == POUR
    assert acheteur.voter(n - 1, -1, 100, 99)[0]["of_delta_cvd"] == CONTRE
    tb = [0.9] * (n - 30) + [0.4] * 30           # 30 dernieres minutes vendeuses, 60 min acheteuses
    mixte = Contexte(bougies([100.0] * n, v=1.0, tb=tb))
    v, info = mixte.voter(n - 1, 1, 100, 101)
    assert info["cvd30"] < 0 < info["cvd60"] and v["of_delta_cvd"] == NEUTRE


def _flux(n, poussee_finale_avance):
    """Une semaine ou le prix suit le delta, puis 15 minutes d'achat agressif."""
    rnd = random.Random(7)
    tb = [rnd.uniform(0.3, 0.7) for _ in range(n)]
    closes, p = [], 100.0
    for k in range(n):
        if k >= n - 15:
            tb[k] = 1.0                                       # delta +1 par minute
            p *= 1 + 0.0004 * poussee_finale_avance
        else:
            p *= 1 + 0.0004 * (2 * tb[k] - 1)                 # 0,04 % par unite de delta
        closes.append(p)
    return Contexte(bougies(closes, v=1.0, tb=tb)), closes


def test_absorption_delta_fort_prix_qui_n_avance_pas():
    n = 8 * 1440
    ctx, closes = _flux(n, 0.0)
    assert ctx.voter(n - 1, 1, closes[-1], closes[-1] * 1.01)[0]["of_absorption"] == CONTRE
    assert ctx.voter(n - 1, -1, closes[-1], closes[-1] * 0.99)[0]["of_absorption"] == NEUTRE


def test_pas_d_absorption_quand_le_prix_suit_le_delta():
    n = 8 * 1440
    ctx, closes = _flux(n, 1.2)
    assert ctx.voter(n - 1, 1, closes[-1], closes[-1] * 1.01)[0]["of_absorption"] == NEUTRE


def test_funding_extreme():
    n = 41 * 1440
    b = bougies([100.0] * n)
    ft = [T0 + 8 * HEURE_MS * k for k in range(41 * 3)]
    fr = [0.0001] * len(ft)
    fr[-1] = 0.0005
    i = n - 1
    ctx = Contexte(b, (ft, fr))
    assert ctx.voter(i, 1, 100, 101)[0]["of_funding"] == CONTRE
    assert ctx.voter(i, -1, 100, 99)[0]["of_funding"] == NEUTRE
    fr2 = list(fr)
    fr2[-1] = -0.0003
    ctx2 = Contexte(b, (ft, fr2))
    assert ctx2.voter(i, -1, 100, 99)[0]["of_funding"] == CONTRE
    assert ctx2.voter(i, 1, 100, 101)[0]["of_funding"] == NEUTRE


def test_le_funding_futur_n_est_pas_vu():
    n = 41 * 1440
    b = bougies([100.0] * n)
    ft = [T0 + 8 * HEURE_MS * k for k in range(41 * 3 + 3)]
    fr = [0.0001] * len(ft)
    fr[-1] = 0.0005                     # regle APRES la derniere bougie
    assert Contexte(b, (ft, fr)).voter(n - 1, 1, 100, 101)[0]["of_funding"] == NEUTRE


# --------------------------------------------------------------------------
# test du hasard
# --------------------------------------------------------------------------

def test_sieges_contre_seul_compares_a_pas_d_objection():
    trades = [{"votes": {"of_funding": v, "liquidite": v}} for v in (NEUTRE, CONTRE, None, POUR)]
    assert analyse.votes_compares(trades, "of_funding") == [POUR, CONTRE, None, POUR]
    assert analyse.votes_compares(trades, "liquidite") == [NEUTRE, CONTRE, None, POUR]


def test_hasard_distingue_un_vrai_lien():
    rnd = random.Random(1)
    r = [rnd.choice([-1.0, 2.0]) for _ in range(400)]
    lie = [POUR if x > 0 else CONTRE for x in r]
    (eb, en), nul = analyse.test_hasard(lie, r, r)
    assert en == 3.0 and analyse.p_valeur(en, [e[1] for e in nul], 0) < 0.01
    sans = [POUR if rnd.random() < 0.5 else CONTRE for _ in r]
    (eb2, en2), nul2 = analyse.test_hasard(sans, r, r)
    assert analyse.p_valeur(en2, [e[1] for e in nul2], 0) > 0.05

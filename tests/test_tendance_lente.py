"""
Tests du moteur de tendance lente (22 nov 2026).

Aucun test ne parle au vrai serveur ni a Binance : fausse plateforme en
memoire (test_sandbox.FakeExchange) et fausses bougies. Le test de parite
rejoue les vraies donnees du backtest (recherche/tendance/donnees) et exige
les memes trades que recherche/tendance/tendance_long.py.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

import pytest

from perps_engine import executor as ex
from perps_engine import sandbox_run as sr
from perps_engine import tendance_lente as T
from test_sandbox import KEY, MARKETS_LIST, POLICY, Clock, FakeExchange

JOUR = 86_400_000
T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)   # un lundi
BTC = "binance|BTCUSDT"
ICI = os.path.dirname(os.path.abspath(__file__))
RECHERCHE = os.path.join(ICI, "..", "recherche", "tendance")


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def barres(closes, t0=T0, im=JOUR, ecart=500.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append((t0 + i * im, o, max(o, c) + ecart, min(o, c) - ecart, c))
        prev = c
    return out


class Bougies:
    """Fausse API Binance : rend les bougies au format klines."""

    def __init__(self, b, im=JOUR):
        self.b, self.im, self.appels = list(b), im, 0

    def __call__(self, url):
        self.appels += 1
        return [[t, str(o), str(h), str(l), str(c), "0", t + self.im - 1]
                for t, o, h, l, c in self.b]


def apres(b, im=JOUR, s=120):
    """Deux minutes apres la cloture de la derniere bougie."""
    return (b[-1][0] + im) / 1000 + s


def tendance_hausse():
    """30 bougies plates, une cassure, puis 25 bougies de hausse."""
    closes = [76_000 + (100 if i % 2 else -100) for i in range(30)]
    closes += [78_000] + [78_000 + 400 * k for k in range(1, 26)]
    return closes


def profil(**kw):
    d = dict(nom="test", compte="FP-TEST", systeme="donchian20", intervalle="1d",
             marche="BTC", stop_atr=3.0, risque_pct=0.005, rattrapage=True)
    d.update(kw)
    return T.ProfilTendance(**d)


def coureur(tmp_path, exch, clock, p, fetch):
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"),
                             fill_wait_seconds=0)
    market = sr.pick_markets(MARKETS_LIST)["BTC"]
    paths = sr.Paths(**{f: str(tmp_path / getattr(sr.Paths(), f))
                        for f in sr.Paths.__dataclass_fields__})
    return T.TendanceRunner(api, exch.account(), market, p, POLICY, paths=paths,
                            fetch=fetch, clock=clock, echo=False,
                            etat_path=str(tmp_path / T.ETAT))


def ordres(exch):
    return [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]


def fermetures(exch):
    return [c for c in exch.calls if c[0] == "POST" and c[1].endswith("/close")]


def demarrer_long(tmp_path):
    b = barres(tendance_hausse())
    clock = Clock(apres(b))
    exch = FakeExchange(clock)
    exch.prices[BTC] = b[-1][4]
    f = Bougies(b)
    r = coureur(tmp_path, exch, clock, profil(), f)
    r.log_start()
    r.cycle()
    return r, exch, clock, f, b


# --------------------------------------------------------------------------
# La fiche
# --------------------------------------------------------------------------

def _ecrire(tmp_path, **kw):
    d = dict(nom="S9", compte="FP-1", systeme="donchian55")
    d.update(kw)
    p = tmp_path / "tendance.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return str(p)


def test_fiche_valide(tmp_path):
    p = T.charger_tendance(_ecrire(tmp_path))
    assert (p.systeme, p.intervalle, p.marche, p.stop_atr, p.risque_pct) == \
        ("donchian55", "1d", "BTC", 3.0, 0.005)


@pytest.mark.parametrize("kw", [dict(compte="1234"), dict(systeme="rsi"),
                                dict(intervalle="1m"), dict(marche="ETH"),
                                dict(stop_atr=0.5), dict(risque_pct=0.02),
                                dict(risque_pct=0), dict(levier=3)])
def test_fiche_refusee(tmp_path, kw):
    with pytest.raises((ValueError, TypeError)):
        T.charger_tendance(_ecrire(tmp_path, **kw))


def test_fiche_absente(tmp_path):
    with pytest.raises(ValueError):
        T.charger_tendance(str(tmp_path / "absente.json"))


# --------------------------------------------------------------------------
# Les regles
# --------------------------------------------------------------------------

def test_fin_de_semaine():
    dimanche = int(datetime(2026, 1, 11, tzinfo=timezone.utc).timestamp() * 1000)
    assert T.fin_de_semaine(dimanche, JOUR)
    assert not T.fin_de_semaine(dimanche - JOUR, JOUR)
    h4 = 14_400_000
    assert T.fin_de_semaine(dimanche + 20 * 3_600_000, h4)
    assert not T.fin_de_semaine(dimanche + 16 * 3_600_000, h4)


def test_signal_donchian():
    b = barres(tendance_hausse())
    assert T.signal(b, 30, "donchian20", JOUR) == 1           # la cassure
    assert T.signal(b, 30, "donchian55", JOUR) is None        # pas assez d'historique
    s = T.signal(b, 40, "donchian20", JOUR)
    assert s == 1 or isinstance(s, tuple)


def test_signal_tsmom_seulement_en_fin_de_semaine():
    b = barres(tendance_hausse())
    for k in range(len(b)):
        s = T.signal(b, k, "tsmom1", JOUR)
        if s is not None:
            assert T.fin_de_semaine(b[k][0], JOUR) and k >= 7
    dimanches = [k for k in range(7, len(b)) if T.fin_de_semaine(b[k][0], JOUR)]
    assert T.signal(b, dimanches[-1], "tsmom1", JOUR) == 1    # en hausse


def test_voulu():
    assert T.voulu(1, 0, None) == 1
    assert T.voulu(1, 0, 1) is None
    assert T.voulu(-1, 0, 1) == -1
    assert T.voulu(("sortie", 110, 90), 89, 1) == 0
    assert T.voulu(("sortie", 110, 90), 95, 1) is None
    assert T.voulu(("sortie", 110, 90), 111, -1) == 0
    assert T.voulu(("sortie", 110, 90), 50, None) is None
    assert T.voulu(None, 0, 1) is None


def _donnees(nom):
    chemin = os.path.join(RECHERCHE, "donnees", nom)
    if not os.path.exists(chemin):
        pytest.skip(f"donnees absentes : {nom}")
    with open(chemin) as f:
        r = csv.reader(f)
        next(r)
        return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in r]


@pytest.mark.parametrize("fichier,im,bps,jpb", [("BTCUSDT_1d.csv", JOUR, 7, 1.0),
                                                ("BTCUSDT_4h.csv", 14_400_000, 42, 1 / 6)])
def test_memes_trades_que_le_backtest(fichier, im, bps, jpb):
    b = _donnees(fichier)
    sys.path.insert(0, RECHERCHE)
    try:
        import tendance_long as R
    finally:
        sys.path.remove(RECHERCHE)
    for systeme in T.SYSTEMES:
        for k_stop in (2.0, 3.0):
            ref = R.simuler(b, "BTC", systeme, k_stop, bps, jpb)
            fin = [x for x in ref if x["motif"] == "fin"]
            ref = [(x["t_entree"], x["t_sortie"], x["sens"], x["motif"])
                   for x in ref if x["motif"] != "fin"]
            trades, pos, _ = T.rejouer(b, systeme, k_stop, im)
            moi = [(x["t"], x["t_sortie"], x["sens"], x["motif"]) for x in trades]
            assert moi == ref, (fichier, systeme, k_stop)
            assert (pos["t"] if pos else None) == (fin[0]["t_entree"] if fin else None)


# --------------------------------------------------------------------------
# Le moteur
# --------------------------------------------------------------------------

def test_premier_demarrage_prend_la_position_du_systeme(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    o = ordres(exch)
    assert len(o) == 1
    body = o[0][2]
    assert body["side"] == "buy"
    assert "take_profit_price" not in body                    # pas d'objectif
    a = T.atr(b)[-1]
    assert body["stop_loss_price"] == pytest.approx(b[-1][4] - 3 * a, abs=0.01)
    assert r.position and r.position["sens"] == 1
    assert r.derniere == b[-1][0]
    ev = [e["event"] for e in _rows(r.paths.trades)]
    assert ev[:3] == ["start", "verdict", "entry_order"]
    v = [e for e in _rows(r.paths.trades) if e["event"] == "verdict"][0]
    assert v["approved"] and v["risk_budget"] == 500.0         # 0,5 % de 100 000
    assert [e for e in _rows(r.paths.signals) if e["event"] == "rattrapage"]
    etat = json.load(open(tmp_path / T.ETAT))
    assert etat["derniere_bougie"] == b[-1][0] and etat["position"]["sens"] == 1


def test_rien_avant_la_bougie_suivante(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    for _ in range(30):
        clock.t += 60
        r.cycle()
    assert f.appels == 1
    assert len(ordres(exch)) == 1


def test_sortie_a_la_cloture_sous_le_plus_bas_de_10_jours(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    bas10 = min(x[3] for x in b[-10:])
    nb = barres([x[4] for x in b] + [bas10 - 100])
    k = len(nb) - 1
    assert isinstance(T.signal(nb, k, "donchian20", JOUR), tuple)   # sortie, pas vente
    f.b = nb
    exch.prices[BTC] = nb[-1][4]
    clock.t = apres(nb)
    rap = r.cycle()
    assert rap["decision"] is None and len(fermetures(exch)) == 1
    assert r.position is None and len(ordres(exch)) == 1
    clock.t += 60
    r.cycle()
    fermes = [e for e in _rows(r.paths.trades) if e["event"] == "position_closed"]
    assert len(fermes) == 1 and fermes[0]["side"] == "buy"
    assert fermes[0]["net"] is not None and fermes[0]["systeme"] == "donchian20"


def test_retournement_ferme_puis_vend(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    bas20 = min(x[3] for x in b[-20:])
    nb = barres([x[4] for x in b] + [bas20 - 500])
    assert T.signal(nb, len(nb) - 1, "donchian20", JOUR) == -1
    f.b = nb
    exch.prices[BTC] = nb[-1][4]
    clock.t = apres(nb)
    r.cycle()
    o = ordres(exch)
    assert len(fermetures(exch)) == 1 and len(o) == 2
    assert o[1][2]["side"] == "sell"
    assert o[1][2]["stop_loss_price"] > nb[-1][4]
    assert r.position["sens"] == -1


def test_stop_touche_pas_de_reentree_sans_nouveau_signal(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    pid = r.position["id"]
    p = exch.open.pop(pid)
    exch.closed.append(dict(p, exit_price=p["entry_price"] - 3000,
                            closed_at=clock.t * 1000, realized_pnl=-480.0,
                            fees=5.0, funding=-1.0, close_reason="stop_loss",
                            status="closed"))
    clock.t += 60
    r.cycle()
    fermes = [e for e in _rows(r.paths.trades) if e["event"] == "position_closed"]
    assert len(fermes) == 1 and fermes[0]["close_reason"] == "stop_loss"
    assert fermes[0]["net"] == -486.0
    # le jour suivant, la tendance continue sans nouvelle cassure : on attend
    nb = barres([x[4] for x in b] + [b[-1][4] - 50])
    assert T.signal(nb, len(nb) - 1, "donchian20", JOUR) != 1
    f.b = nb
    clock.t = apres(nb)
    r.cycle()
    assert len(ordres(exch)) == 1 and r.position is None


def test_sortie_refusee_reessayee_au_cycle_suivant(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    bas10 = min(x[3] for x in b[-10:])
    nb = barres([x[4] for x in b] + [bas10 - 100])
    f.b = nb
    clock.t = apres(nb)
    exch.close_policy = "always_reject"
    with pytest.raises(ex.ApiError):
        r.cycle()
    assert r.position is not None and r.derniere == b[-1][0]   # rien d'avance
    exch.close_policy = "real"
    clock.t += 60
    r.cycle()
    assert r.position is None and r.derniere == nb[-1][0]


def test_prix_deja_au_dela_du_stop_du_systeme(tmp_path):
    b = barres(tendance_hausse())
    clock = Clock(apres(b))
    exch = FakeExchange(clock)
    _, pos, v = T.rejouer(b, "donchian20", 3.0, JOUR)
    assert pos and v is None
    exch.prices[BTC] = pos["stop"] - 10
    r = coureur(tmp_path, exch, clock, profil(), Bougies(b))
    r.cycle()
    assert not ordres(exch) and r.position is None
    ev = [e for e in _rows(r.paths.signals) if e["event"] == "rattrapage"][0]
    assert ev["prise"] is None and "stop" in ev["note"]


def test_rattrapage_desactive(tmp_path):
    b = barres(tendance_hausse())
    clock = Clock(apres(b))
    exch = FakeExchange(clock)
    exch.prices[BTC] = b[-1][4]
    r = coureur(tmp_path, exch, clock, profil(rattrapage=False), Bougies(b))
    r.cycle()
    assert not ordres(exch) and r.derniere == b[-1][0]


def test_position_perdue_adoptee_sans_double_entree(tmp_path):
    b = barres(tendance_hausse())
    clock = Clock(apres(b))
    exch = FakeExchange(clock)
    exch.prices[BTC] = b[-1][4]
    exch.add_manual_position(BTC, side="long", size=0.1)
    r = coureur(tmp_path, exch, clock, profil(), Bougies(b))
    r.cycle()
    assert r.position and r.position["motif"] == "adoptee"
    assert not ordres(exch)


def test_position_d_un_autre_marche_laissee(tmp_path):
    b = barres(tendance_hausse())
    clock = Clock(apres(b))
    exch = FakeExchange(clock)
    exch.prices[BTC] = b[-1][4]
    exch.add_manual_position("binance|XAUUSDT", side="long", size=1.0)
    r = coureur(tmp_path, exch, clock, profil(), Bougies(b))
    r.cycle()
    assert not fermetures(exch) and not ordres(exch)          # une seule position a la fois
    ev = [e["event"] for e in _rows(r.paths.trades)]
    assert "position_etrangere" in ev
    v = [e for e in _rows(r.paths.trades) if e["event"] == "verdict"][0]
    assert not v["approved"] and "POSITION_LIMIT_REACHED" in v["rejects"]


def test_reprise_apres_redemarrage(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    r2 = coureur(tmp_path, exch, clock, profil(), f)
    assert r2.position == r.position and r2.derniere == r.derniere
    clock.t += 60
    r2.cycle()
    assert len(ordres(exch)) == 1


def test_point_toutes_les_15_minutes(tmp_path):
    r, exch, clock, f, b = demarrer_long(tmp_path)
    snaps = [e for e in _rows(r.paths.signals) if e["event"] == "snapshot"]
    assert len(snaps) == 1 and snaps[0]["managed"] == [r.position["id"]]
    assert snaps[0]["markets"]["BTC"]["position"] == 1


# --------------------------------------------------------------------------
# Demarrage : jamais la vraie cle hors d'un dossier de compte
# --------------------------------------------------------------------------

def test_main_sans_cle_hors_du_dossier_comptes(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(sr.KEY_ENV, raising=False)
    monkeypatch.delenv(sr.ACCOUNT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    _ecrire(tmp_path)
    assert not T._sous_comptes(str(tmp_path))
    assert T.main([]) == 2
    assert sr.KEY_ENV not in os.environ
    assert "absente" in capsys.readouterr().out


def test_main_refuse_un_dossier_du_moteur_de_l_or(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _ecrire(tmp_path)
    (tmp_path / "strategie.json").write_text("{}", encoding="utf-8")
    assert T.main([]) == 2
    assert "strategie.json" in capsys.readouterr().out


def test_dossier_de_compte_reconnu():
    assert T._sous_comptes(os.path.join(T.COMPTES, "S9"))
    assert not T._sous_comptes(T.RACINE)

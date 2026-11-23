"""Tests de la v2.7 : une strategie par compte (perps_engine/variantes.py)."""
import json
from dataclasses import replace

import pytest

from perps_engine import executor as ex
from perps_engine import sandbox_run as sr
from perps_engine import variantes as V
from perps_engine.strategy import MomentumStrategy
from test_sandbox import (FAST, KEY, MARKETS_LIST, POLICY, Clock, FakeExchange,
                          _calm, _noise, _rows, _step)

T = 1_800_000_000.0


def profil(**kw):
    base = {"nom": "test", "compte": "FP-1", "marches": ("BTC",), "risque_pct": 0.002}
    base.update(kw)
    return V.Profil(**base)


# -- la fiche -------------------------------------------------------------------

def test_fiche_absente_veut_dire_v26(tmp_path):
    assert V.charger_profil(str(tmp_path / "strategie.json")) is None
    r = sr.reglages(None, {"BTC": 1, "GOLD": 2})
    assert r["paused"] == ("BTC",) and r["hold"] == ("GOLD",)
    assert r["modes"] == {"calendrier": "veto"}
    # 22 nov 2026 (go de Yc) : seul le risque par trade change, 1 % -> 0,3 %
    assert r["policy"] == replace(sr.POLICY, risk_pct_of_balance=0.003)


def test_fiche_lue_et_verifiee(tmp_path):
    f = tmp_path / "strategie.json"
    f.write_text(json.dumps({"nom": "S3", "compte": "FP-00000001", "risque_pct": 0.002,
                             "objectif_r": 3.0}), encoding="utf-8")
    p = V.charger_profil(str(f))
    assert p.marches == ("GOLD",) and p.objectif_r == 3.0 and p.tenir
    r = sr.reglages(p, {"BTC": {"market_id": "b"}, "GOLD": {"market_id": "g"}})
    assert list(r["markets"]) == ["GOLD"] and r["hold"] == ("GOLD",) and r["paused"] == ()
    assert r["policy"].risk_pct_of_balance == 0.002 and r["cfg"].tp_r_multiple == 3.0
    for mauvais in ({"risque_pct": 0.05}, {"compte": "12"}, {"inconnu": 1},
                    {"objectif_r": 1.2}, {"marches": ["SP500"]}):
        d = {"nom": "x", "compte": "FP-1", "risque_pct": 0.002}
        d.update(mauvais)
        f.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises((ValueError, TypeError)):
            V.charger_profil(str(f))


def test_tendance_ajoute_un_siege_en_veto():
    r = sr.reglages(profil(tendance4h=True), {"BTC": {}})
    assert r["tendance"] and r["modes"]["tendance4h"] == "veto"


# -- le siege tendance 4 h et l'ATR -----------------------------------------------

def klines(closes, fin_ms, pas_ms):
    return [[0, c, c + 1, c - 1, c, 0, fin_ms - (len(closes) - k) * pas_ms]
            for k, c in enumerate(closes)]


def test_siege_tendance_vote_selon_la_moyenne_50():
    seat = V.TendanceSeat(fetch=lambda url: klines([100.0] * 60, T * 1000, 4 * 3600_000))
    assert seat.vote({"sym": "GOLD", "side": "buy", "price": 101.0, "now": T})["vote"] == "pour"
    assert seat.vote({"sym": "GOLD", "side": "sell", "price": 101.0, "now": T})["vote"] == "contre"
    assert seat.vote({"sym": "GOLD", "side": "sell", "price": 99.0, "now": T})["vote"] == "pour"


def test_siege_tendance_s_abstient_sans_donnees():
    def panne(url):
        raise RuntimeError("HTTP 503")
    seat = V.TendanceSeat(fetch=panne)
    v = seat.vote({"sym": "GOLD", "side": "buy", "price": 101.0, "now": T})
    assert v["vote"] == "abstient" and "503" in v["raison"]
    court = V.TendanceSeat(fetch=lambda url: klines([100.0] * 20, T * 1000, 4 * 3600_000))
    assert court.vote({"sym": "GOLD", "side": "buy", "price": 101.0, "now": T})["vote"] == "abstient"


def test_bougie_en_cours_ignoree_et_atr():
    data = klines([100.0] * 16, T * 1000, 3600_000)
    data.append([0, 100, 150, 50, 100, 0, T * 1000 + 3600_000])     # pas terminee
    assert len(V.bougies_terminees("GOLD", "1h", 17, T, lambda url: data)) == 16
    assert V.Atr1h(fetch=lambda url: data).valeur("GOLD", T) == pytest.approx(2.0)


# -- la gestion apres l'entree ------------------------------------------------------

def achat():
    return {"side": "buy", "entry": 100.0, "stop": 99.0, "opened_at": T}


def test_moitie_a_plus_un_r():
    p = profil(partiel=True)
    i = achat()
    assert V.gerer(p, i, 100.5, T) is None
    assert V.gerer(p, i, 101.0, T)[0] == "partiel"
    i["partiel_fait"] = True
    assert V.gerer(p, i, 101.5, T) is None


def test_retour_a_plus_un_demi_r_apres_plus_un_r():
    p = profil(verrou_r=0.5)
    i = achat()
    assert V.gerer(p, i, 100.4, T) is None             # +1 R pas encore atteint
    assert V.gerer(p, i, 101.2, T) is None
    assert V.gerer(p, i, 100.5, T)[0] == "fermer"


def test_stop_suiveur_atr_ne_recule_jamais():
    p = profil(suiveur_atr=1.5)
    i = achat()
    assert V.gerer(p, i, 101.0, T, atr=0.4) is None    # suiveur a 101 - 0,6 = 100,4
    assert i["suiveur"] == pytest.approx(100.4)
    assert V.gerer(p, i, 102.0, T, atr=0.4) is None
    assert i["suiveur"] == pytest.approx(101.4)
    assert V.gerer(p, i, 101.8, T, atr=2.0) is None    # ATR plus grand : ne recule pas
    assert i["suiveur"] == pytest.approx(101.4)
    assert V.gerer(p, i, 101.4, T, atr=0.4)[0] == "fermer"


def test_sortie_au_temps_sans_plus_un_r():
    p = profil(sortie_temps_h=8)
    i = achat()
    assert V.gerer(p, i, 100.3, T + 7.9 * 3600) is None
    assert V.gerer(p, i, 100.3, T + 8 * 3600)[0] == "fermer"
    j = achat()
    V.gerer(p, j, 101.1, T + 3600)                     # +1 R atteint : plus de sortie au temps
    assert V.gerer(p, j, 100.3, T + 9 * 3600) is None


# -- dans le moteur, contre la fausse plateforme --------------------------------------

def coureur(tmp_path, exch, clock, p):
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"), fill_wait_seconds=0)
    markets = sr.pick_markets(MARKETS_LIST)
    paths = sr.Paths(**{f: str(tmp_path / getattr(sr.Paths(), f))
                        for f in sr.Paths.__dataclass_fields__})
    return sr.Runner(api, exch.account(), markets, {s: MomentumStrategy(FAST) for s in markets},
                     POLICY, cfg=FAST, paths=paths, echo=False, clock=clock,
                     hold=("BTC",), profil=p)


def ouvrir_un_achat_btc(tmp_path, p):
    clock = Clock()
    exch = FakeExchange(clock)
    r = coureur(tmp_path, exch, clock, p)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    return r, exch, clock


def test_moteur_ferme_la_moitie_a_plus_un_r(tmp_path):
    r, exch, clock = ouvrir_un_achat_btc(tmp_path, profil(partiel=True))
    info = next(iter(r.managed.values()))
    taille = info["size"]
    plus_un_r = info["entry"] + (info["entry"] - info["stop"])
    _step(r, exch, clock, btc=plus_un_r + 1)
    partiels = [b for b in exch.close_bodies if "size" in b]
    assert len(partiels) == 1 and partiels[0]["size"] == pytest.approx(taille / 2, abs=0.001)
    assert len(exch.open) == 1 and len(r.managed) == 1                # la moitie reste
    assert next(iter(r.managed.values()))["partiel_fait"] is True
    _step(r, exch, clock, btc=plus_un_r + 2)
    assert len([b for b in exch.close_bodies if "size" in b]) == 1    # une seule fois
    ev = [e for e in _rows(r.paths.trades) if e["event"] == "exit_managed"]
    assert ev and ev[0]["kind"] == "partiel"


def test_moteur_sort_au_temps(tmp_path):
    r, exch, clock = ouvrir_un_achat_btc(tmp_path, profil(sortie_temps_h=0.05))   # 3 min
    info = next(iter(r.managed.values()))
    for i in range(4):
        _step(r, exch, clock, btc=info["entry"] + 5)
    assert exch.open == {}
    ev = [e for e in _rows(r.paths.trades) if e["event"] == "exit_managed"]
    assert ev and ev[0]["kind"] == "fermer" and "sans +1 R" in ev[0]["reason"]
    assert all("size" not in b for b in exch.close_bodies)           # fermeture complete


def test_entree_confirmee_apres_quinze_minutes(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = coureur(tmp_path, exch, clock, profil(confirmation_min=15))
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert not [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]
    assert "BTC" in r.attente
    for k in range(6, 22):                                           # la tendance continue
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len([c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]) == 1


def test_entree_non_confirmee_si_le_signal_s_eteint(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = coureur(tmp_path, exch, clock, profil(confirmation_min=15))
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    for i in range(20):                                              # retour au calme
        _step(r, exch, clock, btc=_noise(i, 80_300, 10))
    assert not [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]
    assert [e for e in _rows(r.paths.trades) if e["event"] == "entry_unconfirmed"]


def test_sans_fiche_rien_ne_change(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = coureur(tmp_path, exch, clock, None)
    assert r.profil is None and r.atr is None
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    for i in range(12):
        _step(r, exch, clock, btc=_noise(i, 80_300, 10))
    assert len(r.managed) == 1 and not exch.close_bodies             # garde, aucune gestion


def test_partielle_jamais_en_minimal():
    api = ex.SandboxExecutor(KEY, session=FakeExchange(Clock()), sleep=lambda _: None)
    with pytest.raises(ValueError):
        api.close_position("p1", expected_price=100.0, minimal=True, size=1.0)


def test_sans_fiche_le_moteur_ne_va_pas_chercher_la_cle_ailleurs(tmp_path, monkeypatch, capsys):
    # Regression du 21 nov : sans fiche, un lancement hors du dossier du moteur
    # ne doit jamais trouver la vraie cle du .env principal.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PERPS_TEST_KEY", raising=False)
    lus = []
    monkeypatch.setattr(sr, "load_dotenv", lambda path=".env": lus.append(path) or [])
    assert sr.main([]) == 2
    assert lus == [".env"]


def test_avec_fiche_la_cle_vient_du_dossier_du_moteur(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PERPS_TEST_KEY", raising=False)
    (tmp_path / "strategie.json").write_text(
        json.dumps({"nom": "S9", "compte": "FP-00000002", "risque_pct": 0.002}), encoding="utf-8")
    lus = []
    monkeypatch.setattr(sr, "load_dotenv", lambda path=".env": lus.append(path) or [])
    assert sr.main([]) == 2                                   # la fausse lecture ne donne pas de cle
    assert lus[0] == ".env" and lus[1].endswith(".env") and len(lus) == 2
    import os
    assert os.environ["PERPS_SANDBOX_ACCOUNT"] == "FP-00000002"
    monkeypatch.delenv("PERPS_SANDBOX_ACCOUNT", raising=False)

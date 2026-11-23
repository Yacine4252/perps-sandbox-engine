"""Tests du suivi des trades apres leur fermeture."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suivi_trades as S  # noqa: E402
from donnees import Bougies  # noqa: E402


def bougies(*ohlc):
    t = [i * 60_000 for i in range(len(ohlc))]
    o, h, l, c = (list(x) for x in zip(*ohlc))
    return Bougies(t, o, h, l, c, [0.0] * len(t), [0.0] * len(t))


def journal(chemin, lignes):
    chemin.write_text("\n".join(json.dumps(x) for x in lignes) + "\npas du json\n", encoding="utf-8")
    return str(chemin)


def test_vente_tenue_jusqu_a_l_objectif():
    b = bougies((100, 100.5, 99.8, 100.2), (100.2, 100.9, 100.0, 100.4), (100.4, 100.4, 97.5, 98.0))
    sortie, i, prix, ambigu, pire = S.tenir("sell", 100.0, 101.0, 98.0, b, 0)
    assert (sortie, i, prix, ambigu, pire) == ("objectif", 2, 98.0, False, 100.9)


def test_stop_compte_d_abord_si_les_deux_dans_la_meme_minute():
    b = bougies((100, 101.2, 97.9, 99.0))
    assert S.tenir("sell", 100.0, 101.0, 98.0, b, 0)[:4] == ("stop", 0, 101.0, True)


def test_saut_par_dessus_un_niveau_rempli_a_l_ouverture():
    b = bougies((100, 100.2, 99.9, 100.0), (98.5, 98.9, 98.2, 98.6))
    assert S.tenir("buy", 100.0, 99.0, 102.0, b, 0)[:3] == ("stop", 1, 98.5)
    b = bougies((100, 100.2, 99.9, 100.0), (102.4, 102.6, 102.3, 102.5))
    assert S.tenir("buy", 100.0, 99.0, 102.0, b, 0)[:3] == ("objectif", 1, 102.4)


def test_encore_ouvert_et_depart_apres_la_minute_d_entree():
    b = bougies((100, 103, 98.5, 100.0), (100, 100.5, 99.5, 100.3))
    assert S.tenir("buy", 100.0, 99.0, 102.0, b, 1)[:3] == ("encore ouvert", 1, 100.3)


def test_net_avec_frais_et_portage():
    net = S.net_dollars("BTC", "sell", 75742.3, 75165.09, 2.993, 1440)
    brut = (75742.3 - 75165.09) * 2.993
    frais = 0.0003 * 2.993 * (75742.3 + 75165.09)
    portage = 0.000276 * 2.993 * 75742.3
    assert abs(net - (brut - frais - portage)) < 1e-9


def test_mouvement_apres_la_sortie():
    c = [100 + 0.1 * k for k in range(70)]
    b = bougies(*[(x, x + 0.5, x - 0.5, x) for x in c])
    mouv, meilleur, pire = S.apres_sortie(b, 30_000, 100.0, "buy", 2.0)
    # 15 min apres 00:00:30 : derniere bougie fermee = celle de 00:14, cloture 101,4
    assert abs(mouv[0] - 0.7) < 1e-9 and abs(mouv[1] - 2.95) < 1e-9
    assert mouv[2] is None and mouv[3] is None          # 4 h et 24 h pas encore passees
    assert abs(meilleur - 3.7) < 1e-9 and abs(pire + 0.2) < 1e-9
    mouv, meilleur, pire = S.apres_sortie(b, 30_000, 100.0, "sell", 2.0)
    assert abs(mouv[0] + 0.7) < 1e-9 and abs(meilleur - 0.2) < 1e-9 and abs(pire + 3.7) < 1e-9


def test_journal_associe_entree_sortie_et_raison(tmp_path):
    chemin = journal(tmp_path / "trades.log", [
        {"event": "entry_order", "sym": "BTC", "side": "sell", "status": "filled", "size": 2.0,
         "fill_price": 100.0, "stop": 101.0, "tp": 98.0, "ts": "2026-11-08T16:55:51+00:00"},
        {"event": "exit_signal", "position_id": "p1", "sym": "BTC", "reason": "|z| 0.48 < sortie 1.0",
         "ts": "2026-11-08T17:50:58+00:00"},
        {"event": "position_closed", "position_id": "p1", "sym": "BTC", "side": "sell", "entry": 100.0,
         "exit": 100.1, "close_reason": "user", "net": -5.0, "hold_min": 55.1,
         "ts": "2026-11-08T17:52:01+00:00"},
        {"event": "entry_order", "sym": "GOLD", "side": "buy", "status": "rejected", "size": 1.0,
         "fill_price": None, "stop": 1.0, "tp": 3.0, "ts": "2026-11-08T18:00:00+00:00"},
    ])
    tr = S.trades_fermes(chemin, S.ms("2026-11-08T00:00"))
    assert len(tr) == 1
    x = tr[0]
    assert (x["sens"], x["entree"], x["stop"], x["objectif"], x["taille"]) == ("sell", 100.0, 101.0, 98.0, 2.0)
    assert x["raison"] == "|z| 0.48 < sortie 1.0" and x["net"] == -5.0 and x["prix_sortie"] == 100.1
    assert x["t_entree"] == S.ms("2026-11-08T16:55:51+00:00")
    assert x["t_sortie"] == x["t_entree"] + round(55.1 * 60_000)
    assert S.trades_fermes(chemin, S.ms("2026-11-09T00:00")) == []


def test_signaux_refuses_avec_la_fin_du_signal(tmp_path):
    refuse = {"event": "verdict", "sym": "BTC", "side": "buy", "entry": 100.0, "stop": 99.0, "tp": 102.0,
              "approved": False, "rejects": ["TRADE_LIMIT_24H"], "notional": 250.0,
              "ts": "2026-11-09T15:40:20+00:00"}
    trades = journal(tmp_path / "trades.log", [
        refuse,
        dict(refuse, ts="2026-11-09T15:41:20+00:00"),     # meme signal, cycle suivant
        dict(refuse, approved=True, ts="2026-11-09T18:00:00+00:00"),
        dict(refuse, ts="2026-11-08T10:00:00+00:00"),     # avant la date de depart
    ])
    signaux = journal(tmp_path / "signaux.log", [
        {"event": "signal_change", "sym": "BTC", "from": None, "to": "buy", "price": 100.0,
         "ts": "2026-11-09T15:40:00+00:00"},
        {"event": "signal_change", "sym": "GOLD", "from": "buy", "to": None, "price": 7.0,
         "ts": "2026-11-09T15:50:00+00:00"},
        {"event": "signal_change", "sym": "BTC", "from": "buy", "to": None, "price": 99.8,
         "ts": "2026-11-09T16:05:00+00:00"},
    ])
    r = S.signaux_refuses(trades, signaux, S.ms("2026-11-09T00:00"))
    assert len(r) == 1
    x = r[0]
    assert x["refus"] == "TRADE_LIMIT_24H" and x["taille"] == 2.5
    assert x["t_sortie"] == S.ms("2026-11-09T16:05:00+00:00") and x["prix_sortie"] == 99.8


def test_suivi_bout_a_bout():
    b = bougies((100, 100.1, 99.9, 100.0), (100, 100.2, 99.9, 100.1), (100.1, 100.3, 100.0, 100.2),
                (100.2, 100.8, 100.1, 100.7), (100.7, 101.5, 100.6, 101.4), (101.4, 102.3, 101.3, 102.2),
                *[(102.2, 102.4, 102.1, 102.3)] * 4)
    trade = {"sym": "BTC", "sens": "buy", "t_entree": 30_000, "entree": 100.0, "stop": 99.0,
             "objectif": 102.0, "taille": 1.0, "t_sortie": 150_000, "prix_sortie": 100.2, "net": 0.1,
             "raison": "|z| 0.90 < sortie 1.0"}
    refus = [{"sym": "BTC", "sens": "sell", "t_entree": 30_000, "entree": 100.0, "stop": 101.0,
              "objectif": 98.0, "taille": 2.0, "refus": "TRADE_LIMIT_24H", "t_sortie": 270_000,
              "prix_sortie": 100.5},
             {"sym": "BTC", "sens": "sell", "t_entree": 30_000, "entree": 100.0, "stop": 101.0,
              "objectif": 98.0, "taille": 2.0, "refus": "TRADE_LIMIT_24H", "t_sortie": None,
              "prix_sortie": None}]
    lignes = S.suivre([trade], refus, {"BTC": b})
    t, r, actif = lignes
    assert t["type"] == "trade" and t["r"] == 0.1 and t["tenu_issue"] == "objectif"
    assert t["ecart_r"] > 1.5 and t["apres_15min_r"] is None and t["meilleur_24h_r"] > 1.9
    assert r["type"] == "refuse" and r["raison_sortie"] == "fin du signal (estimation)"
    assert r["net_usd"] < -1.0 and r["tenu_issue"] == "stop"
    assert actif["raison_sortie"] == "signal encore actif" and "net_usd" not in actif
    texte = S.resume(lignes, "2026-11-09T00:00", "2026-11-09 00:10")
    assert "TRADES PRIS : 1" in texte and "SIGNAUX REFUSES PAR LE GARDE-FOU : 2" in texte
    assert "Tenir fait mieux 1 fois, moins bien 0 fois" in texte


def test_partie_v26_a_part(tmp_path):
    chemin = tmp_path / "trades.log"
    journal(chemin, [
        {"event": "start", "version": "2.5-sandbox", "ts": "2026-11-06T03:57:00+00:00"},
        {"event": "start", "version": "2.6-sandbox", "ts": "2026-11-20T23:00:30+00:00"},
        {"event": "start", "version": "2.6-sandbox", "ts": "2026-11-21T08:00:00+00:00"}])
    t26 = S.debut_version(str(chemin), "2.6")
    assert t26 == S.ms("2026-11-20T23:00:30+00:00")
    assert S.debut_version(str(chemin), "2.7") is None

    def ligne(type_, quand, refus="", net=100.0):
        return {"type": type_, "sym": "GOLD" if "PAUSE" not in refus else "BTC",
                "sens": "buy", "entree_utc": quand, "refus": refus,
                "net_usd": net, "r": net / 1000, "raison_sortie": "x",
                "sortie_utc": quand, "tenu_net_usd": -net, "tenu_r": -net / 1000,
                "tenu_issue": "stop", "ecart_r": -2 * net / 1000}
    lignes = [ligne("trade", "2026-11-19 10:00", net=500.0),
              ligne("trade", "2026-11-20 23:00", net=200.0),      # minute du demarrage
              ligne("refuse", "2026-11-21 09:00", "MARCHE_EN_PAUSE", -300.0),
              ligne("refuse", "2026-11-21 10:00", "VETO_CALENDRIER", -50.0),
              ligne("refuse", "2026-11-21 11:00", "TRADE_LIMIT_24H", 80.0)]
    texte = S.resume(lignes, "2026-11-05T20:19", "2026-11-21 12:00", t26)
    v26, tout = texte.split("=== DEPUIS LA V2.4")
    assert "DEPUIS LA V2.6" in v26 and "TRADES PRIS : 1" in v26
    assert "Reel : +200 $" in v26
    assert "SIGNAUX BTC EN PAUSE (surveilles, pas trades) : 1" in v26
    assert "SIGNAUX BLOQUES PAR LES NEWS : 1" in v26
    assert "AUTRES SIGNAUX REFUSES PAR LE GARDE-FOU : 1" in v26
    assert "TRADES PRIS : 2" in tout and "Reel : +700 $" in tout
    assert "SIGNAUX REFUSES PAR LE GARDE-FOU : 3" in tout
    sans = S.resume(lignes, "2026-11-05T20:19", "2026-11-21 12:00")
    assert "V2.6" not in sans and "TRADES PRIS : 2" in sans


def test_sortie_rapide_estimee_pour_un_trade_garde():
    b = bougies((100, 100.1, 99.9, 100.0), (100, 100.6, 99.9, 100.5), (100.5, 100.7, 100.4, 100.6),
                (100.6, 102.3, 100.5, 102.2), *[(102.2, 102.4, 102.1, 102.3)] * 3)
    garde = {"sym": "GOLD", "sens": "buy", "t_entree": 30_000, "entree": 100.0, "stop": 99.0,
             "objectif": 102.0, "taille": 10.0, "t_sortie": 210_000, "prix_sortie": 102.0,
             "net": 19.9, "raison": "take_profit"}
    rapide = dict(garde, t_entree=300_000, t_sortie=330_000, prix_sortie=102.2, net=2.0,
                  raison="|z| 0.90 < sortie 1.0")
    fins = {"GOLD": [(20_000, "buy", 99.0),          # avant l'entree : ignoree
                     (120_000, "buy", 100.5),         # le signal s'eteint avant l'objectif
                     (320_000, "buy", 102.2)]}
    S.ajouter_sortie_rapide([garde, rapide], fins)
    assert (garde["t_rapide"], garde["prix_rapide"]) == (120_000, 100.5)
    assert "t_rapide" not in rapide                    # deja sorti sur signal
    g, r = S.suivre([garde, rapide], [], {"GOLD": b})
    # 10 x 0,5 de gain, moins les frais de l'or (0,5 point de base par cote) et le portage
    assert 4.8 < g["rapide_net_usd"] < 5.0 and g["net_usd"] == 19.9
    assert r["rapide_net_usd"] == r["net_usd"] == 2.0
    texte = S.resume([g, r], "2026-11-20T00:00", "2026-11-20 00:10")
    assert "Sortie rapide (estimee) : +7 $" in texte
    assert "sortie rapide estimee +5 $" in texte

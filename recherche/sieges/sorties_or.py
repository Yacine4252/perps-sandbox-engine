"""Cherche, sur l'historique de l'or, ce qui ameliore le moteur v2.6 (or seul,
vrais frais). Meme signal et meme garde-fou que le moteur ; une seule chose
change a la fois. Aucun ordre.

Sorties comparees, a entrees identiques :
  rapide  : la sortie actuelle (v2.4 a v2.6) : le signal retombe sous 1 ou
            s'inverse, sinon stop ou objectif ;
  tenir   : « laisser rouler » : stop ou objectif seulement (2 R) ;
  hybride : couper vite une perte, laisser courir un gain. Quand le signal
            retombe, un trade perdant sort ; un trade gagnant reste, avec son
            stop remonte au prix d'entree, jusqu'a l'objectif ou ce stop.

Regles tirees de la facon de trader de Yc (proposees par Muse, 20 nov 2026),
ajoutees a la sortie rapide :
  vwap          : achats seulement au-dessus du VWAP du jour (remis a zero a
                  00:00 UTC), ventes seulement en dessous ;
  pause2pertes  : apres 2 pertes d'affilee, plus d'entree jusqu'au lendemain
                  00:00 UTC (son gouverneur prop).

Regle de decision fixee AVANT de lancer le calcul (20 nov 2026) : un changement
n'est retenu que s'il fait mieux que la version actuelle, net de frais et de
portage, sur les 8 premiers mois ET sur les mois suivants. En plus, pour une
sortie : l'ecart signal par signal va dans le meme sens sur les deux periodes ;
pour un filtre : les signaux qu'il ecarte perdent sur les deux periodes.
Sinon la version actuelle reste.

Periodes : 11 dec 2025 - 11 aout 2026 (8 mois), 11 aout - 5 nov 2026 (3 mois),
puis depuis le demarrage de la v2.4 (5 nov 2026 20:19 UTC, la periode reelle).

Usage (python du venv du moteur) :
  python -I sorties_or.py <dossier_donnees> <dossier_cache> <dossier_resultats>
"""
import json
import os
import sys
from collections import Counter, deque
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import rejeu  # noqa: E402
from donnees import date_utc  # noqa: E402
from rejeu import COTE, MIN_MS, SR  # noqa: E402
from perps_engine.risk import AccountState, OrderIntent, PlatformLimits, evaluate_order  # noqa: E402

MODES = ("rapide", "tenir", "hybride")
JOUR_MS = 86_400_000
V24_MS = int(datetime(2026, 11, 5, 20, 19, tzinfo=timezone.utc).timestamp() * 1000)  # demarrage v2.4
LIMITES_OR = PlatformLimits(taker_fee_bps=SR.FEE_BPS["GOLD"], max_platform_leverage=50.0)


def _apres_signal(mode, pos, prix):
    """Ce que fait la sortie quand le signal retombe ou s'inverse :
    'sortir', 'garder' ou 'remonter' (stop au prix d'entree)."""
    if mode == "rapide":
        return "sortir"
    if mode == "tenir" or getattr(pos, "be", False):
        return "garder"
    return "sortir" if (prix - pos.entree) * pos.dir <= 0 else "remonter"


def vwap_jour(b):
    """VWAP du jour a la cloture de chaque bougie, remis a zero a 00:00 UTC."""
    out, jour, pv, vol = [], None, 0.0, 0.0
    for k in range(len(b.t)):
        j = b.t[k] // JOUR_MS
        if j != jour:
            jour, pv, vol = j, 0.0, 0.0
        v = b.v[k] or 0.0
        pv += (b.h[k] + b.l[k] + b.c[k]) / 3 * v
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def filtre_vwap(m, vw):
    """bloque(i, sens) : True si le signal va contre le VWAP du jour."""
    def bloque(i, d):
        if vw[i] is None:
            return False
        return (d == 1 and m.c[i] <= vw[i]) or (d == -1 and m.c[i] >= vw[i])
    return bloque


def fidele_or(m, debut_ms, mode, bloque=None, pause2pertes=False):
    """Rejeu de l'or seul avec le garde-fou du moteur (une position a la fois,
    3 trades par 24 h, pause apres une perte), comme rejeu.fidele.
    Rend (trades, refus par motif)."""
    p0 = next(p for p in range(len(m.t)) if m.t[p] >= debut_ms)
    prev_dirs = {m.sym: COTE.get(m.dir[p0 - 1]) if p0 > 0 else None}
    armed, trades, refus, ouvertures = {}, [], Counter(), deque()
    etat = {"pos": None, "perte": None, "serie": 0, "pause": 0}

    def fermer(prix, T, motif, ambigu=False):
        pos = etat["pos"]
        tr = rejeu.cloturer(pos, prix, T, motif, m.frais_bps, ambigu)
        trades.append(tr)
        if tr["r_net"] < 0:
            etat["perte"] = T
            etat["serie"] += 1
            if pause2pertes and etat["serie"] >= 2:
                etat["pause"] = (T // JOUR_MS + 1) * JOUR_MS
                etat["serie"] = 0
        else:
            etat["serie"] = 0
        etat["pos"] = None

    for i in range(p0, len(m.t)):
        T = m.t[i] + MIN_MS
        pos = etat["pos"]
        if pos is not None and i > pos.i:
            x = rejeu.sortie_plateforme(pos, m.o[i], m.h[i], m.l[i])
            if x:
                motif = "stop a l'entree" if (getattr(pos, "be", False) and x[0] == "sl") else x[0]
                fermer(x[1], T, motif, x[2])
        d = COTE.get(m.dir[i])
        for sym, frm, to in SR.update_arming(armed, prev_dirs, {m.sym: d}, T / 1000.0):
            if to in ("buy", "sell") and not m.vu[i]:
                armed.pop(sym, None)                    # ne a la sortie de chauffe
        pos = etat["pos"]
        if pos is not None and d != COTE[pos.dir]:
            quoi = _apres_signal(mode, pos, m.c[i])
            if quoi == "sortir":
                fermer(m.c[i], T, "signal")
            elif quoi == "remonter":
                pos.stop = SR.round_price(pos.entree)
                pos.be = True
        arm = armed.get(m.sym)
        if arm and d == arm["dir"]:
            dd = 1 if arm["dir"] == "buy" else -1
            armed.pop(m.sym, None)                      # une tentative par signal
            if bloque is not None and bloque(i, dd):
                refus["filtre"] += 1
                continue
            if T < etat["pause"]:
                refus["pause 2 pertes"] += 1
                continue
            neuve = rejeu.ouvrir(m, i, dd)
            while ouvertures and ouvertures[0] < T - JOUR_MS:
                ouvertures.popleft()
            st = AccountState(
                equity=100_000, balance=100_000, open_positions=1 if etat["pos"] else 0,
                trades_last_24h=len(ouvertures),
                minutes_since_last_loss=(T - etat["perte"]) / MIN_MS if etat["perte"] else None)
            intent = OrderIntent(market_id=m.sym, side=arm["dir"], entry_price=neuve.entree,
                                 stop_price=neuve.stop, take_profit_price=neuve.objectif)
            v = evaluate_order(intent, st, rejeu.REGLES, LIMITES_OR, SR.POLICY)
            if v.approved:
                etat["pos"] = neuve
                ouvertures.append(T)
            else:
                refus[",".join(v.reject_codes)] += 1
    if etat["pos"] is not None:
        fermer(m.c[-1], m.t[-1] + MIN_MS, "fin")
    return trades, refus


def tous_or(m, debut_ms, mode, bloque=None):
    """Chaque signal neuf devient un trade, sans garde-fou : l'echantillon complet
    pour comparer signal par signal (comme rejeu.tous_les_signaux).
    bloque(i, sens) -> True : le signal est ecarte."""
    trades, ouvertes, prec = [], [], 0
    for i in range(len(m.t)):
        T = m.t[i] + MIN_MS
        restent = []
        for pos in ouvertes:
            x = rejeu.sortie_plateforme(pos, m.o[i], m.h[i], m.l[i]) if pos.i < i else None
            if x:
                motif = "stop a l'entree" if (getattr(pos, "be", False) and x[0] == "sl") else x[0]
                trades.append(rejeu.cloturer(pos, x[1], T, motif, m.frais_bps, x[2]))
            else:
                restent.append(pos)
        ouvertes = restent
        d = m.dir[i]
        neuf = d != prec and d != 0 and m.vu[i]
        prec = d
        restent = []
        for pos in ouvertes:
            if d != pos.dir:
                quoi = _apres_signal(mode, pos, m.c[i])
                if quoi == "sortir":
                    trades.append(rejeu.cloturer(pos, m.c[i], T, "signal", m.frais_bps))
                    continue
                if quoi == "remonter":
                    pos.stop = SR.round_price(pos.entree)
                    pos.be = True
            restent.append(pos)
        ouvertes = restent
        if (neuf and T >= debut_ms and m.dist[i] and not (mode == "rapide" and ouvertes)
                and not (bloque is not None and bloque(i, d))):
            ouvertes.append(rejeu.ouvrir(m, i, d))
    for pos in ouvertes:
        trades.append(rejeu.cloturer(pos, m.c[-1], m.t[-1] + MIN_MS, "fin", m.frais_bps))
    trades.sort(key=lambda x: x["t_entree"])
    return trades


def total_net(trades):
    return sum(x["r_net"] - x["portage_r"] for x in trades)


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    m, b, _ = analyse.charger_marche(dossier, cache, "GOLD")
    fin_ms = m.t[-1] + MIN_MS
    periodes = (("8 mois", analyse.DEBUT_MS, analyse.COUPURE_MS),
                ("3 mois", analyse.COUPURE_MS, V24_MS),
                ("reel v2.4", V24_MS, fin_ms))
    jours = {nom: (fin - debut) / JOUR_MS for nom, debut, fin in periodes}
    bloque_vwap = filtre_vwap(m, vwap_jour(b))
    variantes = {"rapide": dict(mode="rapide"), "tenir": dict(mode="tenir"),
                 "hybride": dict(mode="hybride"),
                 "vwap": dict(mode="rapide", bloque=bloque_vwap),
                 "pause2pertes": dict(mode="rapide", pause2pertes=True)}
    res = {"periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)], "fidele": {}, "tous": {}}

    for nom_v, kw in variantes.items():
        tr, refus = fidele_or(m, analyse.DEBUT_MS, **kw)
        CS.ajouter_portage(tr)
        r = {"refus": dict(refus), "sorties": dict(Counter(x["sortie"] for x in tr))}
        for nom, debut, fin in periodes:
            lot = [x for x in tr if debut <= x["t_entree"] < fin]
            r[nom] = CS.resume(lot, jours[nom])
            r[nom]["total_net_portage"] = total_net(lot)
        r["par_mois"] = {k: v["total"] for k, v in analyse.par_mois(tr).items()}
        res["fidele"][nom_v] = r

    print(f"=== OR SEUL, REJEU FIDELE ({res['periode'][0]} -> {res['periode'][1]}) : "
          f"meme signal, meme garde-fou, une seule chose change")
    for nom, _, _ in periodes:
        for nom_v in variantes:
            CS.imprimer(f"{nom} | {nom_v}", res["fidele"][nom_v][nom])
        print("   total net portage compris : " + "  ".join(
            f"{nom_v} {res['fidele'][nom_v][nom]['total_net_portage']:+.1f} R" for nom_v in variantes))
        print()
    mois = sorted(res["fidele"]["rapide"]["par_mois"])
    pr = res["fidele"]["rapide"]["par_mois"]
    for nom_v in variantes:
        if nom_v == "rapide":
            continue
        pm = res["fidele"][nom_v]["par_mois"]
        mieux = sum(1 for k in mois if pm.get(k, 0) > pr.get(k, 0))
        res["fidele"][nom_v]["mois_meilleurs"] = mieux
        print(f"{nom_v} fait mieux que rapide {mieux} mois sur {len(mois)} : " + "  ".join(
            f"{k[2:]} {pr.get(k, 0):+.1f}/{pm.get(k, 0):+.1f}" for k in mois))
    for nom_v in variantes:
        print(f"{nom_v} : sorties {res['fidele'][nom_v]['sorties']} ; refus {res['fidele'][nom_v]['refus']}")

    print("\n=== TOUS LES SIGNAUX D'OR (sans garde-fou), net de frais et de portage ; "
          "p par grappes d'une semaine")
    tous = {mode: tous_or(m, analyse.DEBUT_MS, mode) for mode in MODES}
    for mode in MODES:
        CS.ajouter_portage(tous[mode])
    for mode in ("tenir", "hybride"):
        res["tous"][mode] = {}
        for nom, debut, fin in periodes:
            paires = [p for p in CS.apparier(tous["rapide"], tous[mode]) if debut <= p[0] < fin]
            e = CS.ecart_par_semaine(paires, 3)
            res["tous"][mode][nom] = e
            if e:
                print(f"   sortie {mode:8} {nom:10} {e['n']:5} signaux  ecart {e['ecart']:+.3f} R "
                      f"par signal (p {e['p']:.3f}, {e['semaines']} semaines)")
    gardes = {x["t_entree"] for x in tous_or(m, analyse.DEBUT_MS, "rapide", bloque=bloque_vwap)}
    res["tous"]["vwap"] = {}
    for nom, debut, fin in periodes:
        lot = [x for x in tous["rapide"] if debut <= x["t_entree"] < fin]
        ecartes = [x for x in lot if x["t_entree"] not in gardes]
        gardes_p = [x for x in lot if x["t_entree"] in gardes]
        e = {"ecartes": len(ecartes), "ecartes_total": total_net(ecartes),
             "gardes": len(gardes_p), "gardes_total": total_net(gardes_p)}
        res["tous"]["vwap"][nom] = e
        print(f"   filtre vwap   {nom:10} ecarte {e['ecartes']:4} signaux ({e['ecartes_total']:+.1f} R), "
              f"garde {e['gardes']:4} ({e['gardes_total']:+.1f} R)")

    # La regle de decision, appliquee telle quelle.
    f = res["fidele"]
    verdict = {}
    for nom_v in variantes:
        if nom_v == "rapide":
            continue
        ok = all(f[nom_v][p]["total_net_portage"] > f["rapide"][p]["total_net_portage"]
                 for p in ("8 mois", "3 mois"))
        if nom_v in ("tenir", "hybride"):
            ok = ok and all(res["tous"][nom_v][p] and res["tous"][nom_v][p]["ecart"] > 0
                            for p in ("8 mois", "3 mois"))
        if nom_v == "vwap":
            ok = ok and all(res["tous"]["vwap"][p]["ecartes_total"] < 0 for p in ("8 mois", "3 mois"))
        verdict[nom_v] = ok
    res["verdict"] = verdict
    print("\nREGLE (mieux sur 8 mois ET sur 3 mois) : " + ", ".join(
        f"{k} {'PASSE' if v else 'ne passe pas'}" for k, v in verdict.items()))
    with open(os.path.join(sortie, "sorties_or.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

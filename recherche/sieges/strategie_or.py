"""Cherche la strategie a tester en premier sur le compte Sandbox 2 (demande de
Yc via Muse, 21 nov 2026). Base : la v2.6 sur l'or (momentum du moteur, tenu
jusqu'au stop ou a l'objectif 2 R). Une seule chose change a la fois. Aucun ordre.

Variantes, choisies AVANT le calcul pour une raison de marche, pas en
fouillant les donnees :
  tenir         : la v2.6 (base) ;
  objectif3R    : objectif a 3 R (laisser plus de place aux tendances de l'or) ;
  objectif1.5R  : objectif a 1,5 R (plus souvent touche) ;
  z2.5          : entree seulement si le mouvement depasse 2,5 ecarts-types
                  (signaux plus forts, moins nombreux) ;
  seance        : entrees seulement de 07:00 a 17:00 UTC (Londres et New York,
                  la ou l'or a du volume) ;
  temps8h       : sortie apres 8 h sans +1 R (a surveiller depuis gestion_or.py).

Regle de decision fixee AVANT le calcul : une variante ne bat « tenir » que si
elle fait mieux, net de frais et de portage, sur les 8 premiers mois ET sur les
mois suivants. Avec 5 variantes, une peut passer par hasard : le compte
Sandbox 2 sert justement a la confirmer en vrai.

Le veto des news ne peut pas etre rejoue (le moteur ne garde que 10 jours de
calendrier). Le drawdown est mesure pour fixer le risque par trade : le compte
sandbox coupe a -6 % (plancher statique) et a -4 % dans la journee.

Usage : python -I strategie_or.py <dossier_donnees> <dossier_cache> <dossier_resultats>
"""
import json
import os
import sys
from collections import Counter, deque
from dataclasses import replace

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import gestion_or as G  # noqa: E402
import rejeu  # noqa: E402
from donnees import date_utc  # noqa: E402
from rejeu import COTE, MIN_MS, SR  # noqa: E402
from sorties_or import JOUR_MS, LIMITES_OR, V24_MS  # noqa: E402
from perps_engine.risk import AccountState, OrderIntent, evaluate_order  # noqa: E402


def directions(m, entree_z, sortie_z=1.0):
    """Recalcule le signal du moteur pour un autre seuil d'entree, a partir du z
    du moteur : ouvre au-dela de entree_z, ferme sous sortie_z (hysteresis)."""
    out, d = [], 0
    for z, dist in zip(m.z, m.dist):
        if z is None or not dist:
            d = 0
        elif d == 0:
            d = 1 if z >= entree_z else (-1 if z <= -entree_z else 0)
        elif d == 1:
            d = -1 if z <= -entree_z else (0 if z < sortie_z else 1)
        else:
            d = 1 if z >= entree_z else (0 if z > -sortie_z else -1)
        out.append(d)
    return out


def ouvrir(m, i, d, tp_r):
    entree = m.c[i]
    dist = m.dist[i]
    pos = rejeu.Position(m.sym, d, entree, SR.round_price(entree - d * dist),
                         SR.round_price(entree + d * dist * tp_r), dist, i, m.t[i] + MIN_MS, m.z[i])
    pos.risque = abs(entree - pos.stop)
    pos.reste, pos.sorties, pos.mfav = 1.0, [], 0.0
    pos.suiveur = pos.partiel = False
    return pos


def fidele(m, debut_ms, regle="tenir", tp_r=2.0, heures=None):
    p0 = next(p for p in range(len(m.t)) if m.t[p] >= debut_ms)
    prev_dirs = {m.sym: COTE.get(m.dir[p0 - 1]) if p0 > 0 else None}
    armed, trades, refus, ouvertures = {}, [], Counter(), deque()
    pos, perte = None, None
    for i in range(p0, len(m.t)):
        T = m.t[i] + MIN_MS
        if pos is not None and i > pos.i:
            G.avancer(pos, m, i, regle)
            if pos.reste <= 0:
                tr = G.bilan(pos, m.frais_bps)
                trades.append(tr)
                if tr["r_net"] < 0:
                    perte = T
                pos = None
        d = COTE.get(m.dir[i])
        for sym, frm, to in SR.update_arming(armed, prev_dirs, {m.sym: d}, T / 1000.0):
            if to in ("buy", "sell") and not m.vu[i]:
                armed.pop(sym, None)
        arm = armed.get(m.sym)
        if arm and d == arm["dir"]:
            armed.pop(m.sym, None)
            if heures and not (heures[0] <= (T // 3_600_000) % 24 < heures[1]):
                refus["hors seance"] += 1
                continue
            dd = 1 if arm["dir"] == "buy" else -1
            neuve = ouvrir(m, i, dd, tp_r)
            while ouvertures and ouvertures[0] < T - JOUR_MS:
                ouvertures.popleft()
            st = AccountState(equity=100_000, balance=100_000, open_positions=1 if pos else 0,
                              trades_last_24h=len(ouvertures),
                              minutes_since_last_loss=(T - perte) / MIN_MS if perte else None)
            v = evaluate_order(OrderIntent(market_id=m.sym, side=arm["dir"], entry_price=neuve.entree,
                                           stop_price=neuve.stop, take_profit_price=neuve.objectif),
                               st, rejeu.REGLES, LIMITES_OR, SR.POLICY)
            if v.approved:
                pos = neuve
                ouvertures.append(T)
            else:
                refus[",".join(v.reject_codes)] += 1
    if pos is not None:
        G.fin_de_donnees(pos, m)
        trades.append(G.bilan(pos, m.frais_bps))
    return trades, refus


def pire_journee(lot):
    jours = Counter()
    for x in lot:
        jours[x["t_sortie"] // JOUR_MS] += x["r_net"] - x["portage_r"]
    return min(jours.values()) if jours else 0.0


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    m = analyse.charger_marche(dossier, cache, "GOLD")[0]
    d2 = directions(m, 2.0)
    accord = sum(1 for a, b in zip(d2, m.dir) if a == b) / len(d2)
    m25 = replace(m, dir=directions(m, 2.5))
    fin_ms = m.t[-1] + MIN_MS
    periodes = (("8 mois", analyse.DEBUT_MS, analyse.COUPURE_MS),
                ("3 mois", analyse.COUPURE_MS, V24_MS),
                ("15 jours", V24_MS, fin_ms))
    variantes = {"tenir": (m, {}), "objectif3R": (m, {"tp_r": 3.0}), "objectif1.5R": (m, {"tp_r": 1.5}),
                 "z2.5": (m25, {}), "seance": (m, {"heures": (7, 17)}), "temps8h": (m, {"regle": "temps8h"})}
    res = {"periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)],
           "accord_signal_recalcule_z2": accord, "variantes": {}, "verdict": {}}
    print(f"=== OR SEUL ({res['periode'][0]} -> {res['periode'][1]}), garde-fou du moteur, net de frais et de "
          f"portage, en R. Signal recalcule a z 2,0 : {accord * 100:.2f} % des minutes identiques au moteur.")
    for nom, (mm, kw) in variantes.items():
        tr, refus = fidele(mm, analyse.DEBUT_MS, **kw)
        CS.ajouter_portage(tr)
        r = {p: G.chiffres([x for x in tr if a <= x["t_entree"] < b]) for p, a, b in periodes}
        r["tout"] = G.chiffres(tr)
        r["pire_journee"] = pire_journee(tr)
        r["refus"] = dict(refus.most_common(4))
        res["variantes"][nom] = r
        print(f"{nom:12} " + " | ".join(
            f"{p} : {r[p]['total']:+6.1f} R ({r[p]['n']:3} tr, {r[p]['gagnants_pct']:4.1f} %, DD {r[p]['dd']:4.1f})"
            for p, _, _ in periodes)
            + f" | 11,5 mois : {r['tout']['total']:+.1f} R, DD max {r['tout']['dd']:.1f} R, pire jour {r['pire_journee']:+.1f} R")
    base = res["variantes"]["tenir"]
    for nom in list(variantes)[1:]:
        r = res["variantes"][nom]
        res["verdict"][nom] = all(r[p]["total"] > base[p]["total"] for p in ("8 mois", "3 mois"))
    print("\nREGLE (mieux que tenir sur 8 mois ET sur 3 mois) : " + ", ".join(
        f"{k} {'BAT TENIR' if v else 'ne bat pas'}" for k, v in res["verdict"].items()))
    for nom in variantes:
        print(f"   refus {nom} : {res['variantes'][nom]['refus']}")
    with open(os.path.join(sortie, "strategie_or.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

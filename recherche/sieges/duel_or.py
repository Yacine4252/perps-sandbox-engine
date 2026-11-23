"""Duel d'idees sur l'or (demande de Yc via Muse, 21 nov 2026) : les 3 idees de
Muse contre la v2.6 (« tenir » : stop ou objectif 2 R). Meme signal et meme
garde-fou que le moteur, or seul, frais reels. Aucun ordre.

  tenir       : la v2.6 (base) ;
  tendance4h  : signal pris seulement dans le sens de la tendance 4 h : achat si
                le prix est au-dessus de la moyenne mobile 50 des clotures 4 h,
                vente s'il est en dessous (bougie 4 h terminee, pas de regard
                vers l'avenir) ;
  confirme15  : 15 min apres la naissance du signal, entree seulement si le z
                est encore au-dela de 1,5 dans le meme sens ; stop et objectif
                calcules a ce moment-la ;
  trailing    : a +1 R, plus d'objectif ; stop suiveur a 1,5 ATR 1 h (moyenne
                des 14 dernieres heures terminees) sous le plus haut atteint
                (au-dessus du plus bas pour une vente), qui ne recule jamais.
                Sortie seulement au stop.

Bougie ambigue : le stop d'abord ; un stop deplace vaut a partir de la minute
suivante. Le veto des news ne peut pas etre rejoue.

Regle de decision fixee AVANT le calcul : une idee ne bat « tenir » que si elle
fait mieux, net de frais et de portage, sur les 8 premiers mois ET sur les mois
suivants.

Usage : python -I duel_or.py <dossier_donnees> <dossier_cache> <dossier_resultats>
"""
import json
import os
import sys
from collections import Counter, deque

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import gestion_or as G  # noqa: E402
import rejeu  # noqa: E402
import strategie_or as S  # noqa: E402
from donnees import date_utc  # noqa: E402
from rejeu import MIN_MS, SR  # noqa: E402
from sorties_or import JOUR_MS, LIMITES_OR, V24_MS  # noqa: E402
from perps_engine.risk import AccountState, OrderIntent, evaluate_order  # noqa: E402

# idee -> objectif en R. objectif1.5R : 1,502 R, pour que l'arrondi des prix ne
# passe pas sous le minimum de 1,5 du garde-fou (le premier test etait fausse).
IDEES = {"tenir": 2.0, "tendance4h": 2.0, "confirme15": 2.0, "trailing": 2.0,
         "tendance4h_3R": 3.0, "objectif1.5R": 1.502}


def par_periode(m, minutes):
    """Pour chaque minute : bougies de `minutes` deja terminees (o, h, l, c),
    sous forme de liste, et l'indice de la derniere terminee (-1 si aucune)."""
    pas = minutes * MIN_MS
    bougies, derniere, cour = [], [], None
    for i in range(len(m.t)):
        k = m.t[i] // pas
        if cour is None or k != cour[0]:
            if cour is not None:
                bougies.append(cour[1:])
            cour = [k, m.o[i], m.h[i], m.l[i], m.c[i]]
        else:
            cour[2] = max(cour[2], m.h[i])
            cour[3] = min(cour[3], m.l[i])
            cour[4] = m.c[i]
        derniere.append(len(bougies) - 1)
    return bougies, derniere


def mm50_4h(m):
    b, der = par_periode(m, 240)
    clot = [x[3] for x in b]
    mm, somme = [], 0.0
    for k in range(len(clot)):
        somme += clot[k]
        if k >= 50:
            somme -= clot[k - 50]
        mm.append(somme / 50 if k >= 49 else None)
    return [mm[j] if j >= 0 else None for j in der]


def atr_1h(m, n=14):
    b, der = par_periode(m, 60)
    tr = []
    for k, (o, h, l, c) in enumerate(b):
        pc = b[k - 1][3] if k else c
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr, somme = [], 0.0
    for k in range(len(tr)):
        somme += tr[k]
        if k >= n:
            somme -= tr[k - n]
        atr.append(somme / n if k >= n - 1 else None)
    return [atr[j] if j >= 0 else None for j in der]


def avancer_trailing(pos, m, i, atr):
    o, h, l, c = m.o[i], m.h[i], m.l[i], m.c[i]
    d, R, T = pos.dir, pos.risque, m.t[i] + MIN_MS
    if (l <= pos.stop) if d == 1 else (h >= pos.stop):
        prix = min(o, pos.stop) if d == 1 else max(o, pos.stop)
        pos.sorties.append((1.0, prix, T, "trailing" if pos.suiveur else "sl"))
        pos.reste = 0.0
        return
    if not pos.suiveur:
        if (h >= pos.entree + R) if d == 1 else (l <= pos.entree - R):
            pos.suiveur, pos.extreme = True, (h if d == 1 else l)
    else:
        pos.extreme = max(pos.extreme, h) if d == 1 else min(pos.extreme, l)
    if pos.suiveur and atr[i]:
        nouveau = SR.round_price(pos.extreme - d * 1.5 * atr[i])
        pos.stop = max(pos.stop, nouveau) if d == 1 else min(pos.stop, nouveau)


def fidele(m, debut_ms, idee, mm=None, atr=None, tp_r=2.0):
    """Or seul, garde-fou du moteur : une tentative par signal ne, une position a
    la fois, 3 trades par 24 h, pause apres une perte."""
    p0 = next(p for p in range(len(m.t)) if m.t[p] >= debut_ms)
    trades, refus, ouvertures, attente = [], Counter(), deque(), []
    pos, perte, prec = None, None, (m.dir[p0 - 1] if p0 else 0)
    for i in range(p0, len(m.t)):
        T = m.t[i] + MIN_MS
        if pos is not None and i > pos.i:
            if idee == "trailing":
                avancer_trailing(pos, m, i, atr)
            else:
                G.avancer(pos, m, i, "tenir")
            if pos.reste <= 0:
                tr = G.bilan(pos, m.frais_bps)
                trades.append(tr)
                if tr["r_net"] < 0:
                    perte = T
                pos = None
        d = m.dir[i]
        neuf = d != prec and d != 0 and m.vu[i] and m.dist[i]
        prec = d
        candidats = []
        if neuf:
            if idee == "confirme15":
                attente.append((i + 15, d))
            else:
                candidats.append((i, d))
        if attente and attente[0][0] == i:
            j, dd = attente.pop(0)
            if m.z[i] is not None and m.z[i] * dd >= 1.5 and m.dist[i]:
                candidats.append((i, dd))
            else:
                refus["non confirme"] += 1
        for ii, dd in candidats:
            if idee.startswith("tendance4h"):
                if mm[ii] is None or (dd == 1 and m.c[ii] <= mm[ii]) or (dd == -1 and m.c[ii] >= mm[ii]):
                    refus["contre la tendance 4 h"] += 1
                    continue
            neuve = S.ouvrir(m, ii, dd, tp_r)
            while ouvertures and ouvertures[0] < T - JOUR_MS:
                ouvertures.popleft()
            st = AccountState(equity=100_000, balance=100_000, open_positions=1 if pos else 0,
                              trades_last_24h=len(ouvertures),
                              minutes_since_last_loss=(T - perte) / MIN_MS if perte else None)
            v = evaluate_order(OrderIntent(market_id=m.sym, side="buy" if dd == 1 else "sell",
                                           entry_price=neuve.entree, stop_price=neuve.stop,
                                           take_profit_price=neuve.objectif),
                               st, rejeu.REGLES, LIMITES_OR, SR.POLICY)
            if v.approved:
                if idee == "trailing":
                    neuve.objectif = neuve.entree + dd * 1e9      # plus d'objectif fixe
                pos = neuve
                ouvertures.append(T)
            else:
                refus[",".join(v.reject_codes)] += 1
    if pos is not None:
        G.fin_de_donnees(pos, m)
        trades.append(G.bilan(pos, m.frais_bps))
    return trades, refus


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    m = analyse.charger_marche(dossier, cache, "GOLD")[0]
    mm, atr = mm50_4h(m), atr_1h(m)
    fin_ms = m.t[-1] + MIN_MS
    periodes = (("8 mois", analyse.DEBUT_MS, analyse.COUPURE_MS),
                ("3 mois", analyse.COUPURE_MS, V24_MS),
                ("15 jours", V24_MS, fin_ms))
    res = {"periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)], "idees": {}, "verdict": {}}
    print(f"=== DUEL D'IDEES, OR SEUL ({res['periode'][0]} -> {res['periode'][1]}), garde-fou du moteur, "
          "net de frais et de portage, en R")
    for idee, tp_r in IDEES.items():
        tr, refus = fidele(m, analyse.DEBUT_MS, idee, mm, atr, tp_r)
        CS.ajouter_portage(tr)
        r = {p: G.chiffres([x for x in tr if a <= x["t_entree"] < b]) for p, a, b in periodes}
        r["tout"] = G.chiffres(tr)
        r["pire_journee"] = S.pire_journee(tr)
        r["refus"] = dict(refus.most_common(4))
        r["sorties"] = dict(Counter(x["sortie"] for x in tr).most_common(4))
        r["duree_mediane_min"] = sorted(x["duree_min"] for x in tr)[len(tr) // 2] if tr else None
        res["idees"][idee] = r
        print(f"{idee:11} " + " | ".join(
            f"{p} : {r[p]['total']:+6.1f} R ({r[p]['n']:3} tr, {r[p]['gagnants_pct']:4.1f} %, DD {r[p]['dd']:4.1f})"
            for p, _, _ in periodes)
            + f" | 11,5 mois : {r['tout']['total']:+.1f} R, DD max {r['tout']['dd']:.1f} R, pire jour "
              f"{r['pire_journee']:+.1f} R, duree mediane {r['duree_mediane_min']:.0f} min")
    base = res["idees"]["tenir"]
    for idee in list(IDEES)[1:]:
        r = res["idees"][idee]
        res["verdict"][idee] = all(r[p]["total"] > base[p]["total"] for p in ("8 mois", "3 mois"))
    print("\nREGLE (mieux que tenir sur 8 mois ET sur 3 mois) : " + ", ".join(
        f"{k} {'BAT TENIR' if v else 'ne bat pas'}" for k, v in res["verdict"].items()))
    for idee in IDEES:
        print(f"   {idee} : refus {res['idees'][idee]['refus']} ; sorties {res['idees'][idee]['sorties']}")
    with open(os.path.join(sortie, "duel_or.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

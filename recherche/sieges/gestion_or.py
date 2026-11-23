"""Teste trois idees de gestion du stop sur l'or, contre la sortie de la v2.6
(« tenir » : stop ou objectif 2 R). Meme signal et meme garde-fou que le moteur,
or seul, frais reels. Aucun ordre, moteur non modifie (demande de Yc via Muse,
21 nov 2026, pour le bilan du 29 nov).

  tenir     : la v2.6, stop ou objectif 2 R ;
  partiel   : la moitie fermee a +1 R, l'autre moitie jusqu'au stop ou a +2 R ;
  suiveur   : quand le trade atteint +1 R, stop remonte a +0,5 R, puis stop ou +2 R ;
  temps4h   : sans +1 R atteint apres 4 h, sortie au prix du moment ; sinon stop ou +2 R ;
  temps8h   : pareil apres 8 h.

Bougie ambigue (le stop et un niveau favorable touches dans la meme minute) :
on compte le stop d'abord, comme partout dans ce dossier. Un stop deplace ne
s'applique qu'a partir de la minute suivante.

Le veto des news ne peut pas etre rejoue : le moteur ne garde que 10 jours de
calendrier. Les autres regles du garde-fou sont celles du moteur.

Regle de decision fixee AVANT le calcul : une idee ne bat « tenir » que si elle
fait mieux, net de frais et de portage, sur les 8 premiers mois ET sur les mois
suivants, et si l'ecart signal par signal (tous les signaux, sans garde-fou) va
dans le meme sens sur les deux periodes.

Usage : python -I gestion_or.py <dossier_donnees> <dossier_cache> <dossier_resultats>
"""
import json
import os
import sys
from collections import Counter, deque

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import rejeu  # noqa: E402
from donnees import date_utc  # noqa: E402
from rejeu import COTE, MIN_MS, SR  # noqa: E402
from sorties_or import JOUR_MS, LIMITES_OR, V24_MS  # noqa: E402
from perps_engine.risk import AccountState, OrderIntent, evaluate_order  # noqa: E402

REGLES = ("tenir", "partiel", "suiveur", "temps4h", "temps8h")
LIMITE_TEMPS = {"temps4h": 240, "temps8h": 480}


def nouvelle(m, i, d):
    pos = rejeu.ouvrir(m, i, d)
    pos.reste, pos.sorties, pos.mfav = 1.0, [], 0.0
    pos.suiveur = pos.partiel = False
    return pos


def avancer(pos, m, i, regle):
    """Fait vivre la position pendant la bougie i (apres celle d'entree).
    Ajoute ses sorties a pos.sorties : (fraction, prix, instant, motif)."""
    o, h, l, c = m.o[i], m.h[i], m.l[i], m.c[i]
    d, R, T = pos.dir, pos.risque, m.t[i] + MIN_MS
    def touche(niveau):
        return h >= niveau if d == 1 else l <= niveau
    stop_touche = l <= pos.stop if d == 1 else h >= pos.stop
    if stop_touche:                                    # le stop d'abord (prudent)
        prix = min(o, pos.stop) if d == 1 else max(o, pos.stop)
        pos.sorties.append((pos.reste, prix, T, "stop suiveur" if pos.suiveur else "sl"))
        pos.reste = 0.0
        return
    un_r = pos.entree + d * R
    if regle == "partiel" and not pos.partiel and touche(un_r):
        pos.sorties.append((0.5, un_r, T, "+1R"))
        pos.reste, pos.partiel = 0.5, True
    if touche(pos.objectif):
        pos.sorties.append((pos.reste, pos.objectif, T, "tp"))
        pos.reste = 0.0
        return
    fav = (h - pos.entree) * d if d == 1 else (pos.entree - l)
    pos.mfav = max(pos.mfav, fav)
    if regle == "suiveur" and not pos.suiveur and pos.mfav >= R:
        pos.stop = SR.round_price(pos.entree + d * 0.5 * R)   # valable des la minute suivante
        pos.suiveur = True
    lim = LIMITE_TEMPS.get(regle)
    if lim and pos.mfav < R and (T - pos.t) >= lim * MIN_MS:
        pos.sorties.append((pos.reste, c, T, "temps"))
        pos.reste = 0.0


def bilan(pos, frais_bps):
    """Le trade ferme, en R, au format de rejeu.cloturer."""
    brut = sum(f * (p - pos.entree) * pos.dir for f, p, _, _ in pos.sorties) / pos.risque
    frais = sum(f * frais_bps / 1e4 * (pos.entree + p) for f, p, _, _ in pos.sorties) / pos.risque
    duree = sum(f * (t - pos.t) for f, _, t, _ in pos.sorties) / MIN_MS   # ponderee : sert au portage
    der = pos.sorties[-1]
    return {"sym": pos.sym, "sens": pos.dir, "t_entree": pos.t, "i_entree": pos.i,
            "entree": pos.entree, "stop": pos.stop, "objectif": pos.objectif,
            "risque": pos.risque, "risque_bps": pos.risque / pos.entree * 1e4, "z": pos.z,
            "t_sortie": der[2], "prix_sortie": der[1],
            "sortie": "+".join(s[3] for s in pos.sorties), "ambigu": False,
            "r_brut": brut, "frais_r": frais, "r_net": brut - frais, "duree_min": duree}


def fin_de_donnees(pos, m):
    if pos.reste > 0:
        pos.sorties.append((pos.reste, m.c[-1], m.t[-1] + MIN_MS, "fin"))
        pos.reste = 0.0


def fidele(m, debut_ms, regle):
    """Or seul, garde-fou du moteur (une position a la fois, 3 trades par 24 h,
    pause apres une perte), sans sortie sur signal."""
    p0 = next(p for p in range(len(m.t)) if m.t[p] >= debut_ms)
    prev_dirs = {m.sym: COTE.get(m.dir[p0 - 1]) if p0 > 0 else None}
    armed, trades, refus, ouvertures = {}, [], Counter(), deque()
    pos, perte = None, None
    for i in range(p0, len(m.t)):
        T = m.t[i] + MIN_MS
        if pos is not None and i > pos.i:
            avancer(pos, m, i, regle)
            if pos.reste <= 0:
                tr = bilan(pos, m.frais_bps)
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
            dd = 1 if arm["dir"] == "buy" else -1
            neuve = nouvelle(m, i, dd)
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
        fin_de_donnees(pos, m)
        trades.append(bilan(pos, m.frais_bps))
    return trades, refus


def tous(m, debut_ms, regle):
    """Chaque signal neuf devient un trade, sans garde-fou (comparaison signal par signal)."""
    trades, ouvertes, prec = [], [], 0
    for i in range(len(m.t)):
        restent = []
        for pos in ouvertes:
            if pos.i < i:
                avancer(pos, m, i, regle)
            if pos.reste <= 0:
                trades.append(bilan(pos, m.frais_bps))
            else:
                restent.append(pos)
        ouvertes = restent
        d = m.dir[i]
        if d != prec and d != 0 and m.vu[i] and m.t[i] + MIN_MS >= debut_ms and m.dist[i]:
            ouvertes.append(nouvelle(m, i, d))
        prec = d
    for pos in ouvertes:
        fin_de_donnees(pos, m)
        trades.append(bilan(pos, m.frais_bps))
    trades.sort(key=lambda x: x["t_entree"])
    return trades


def chiffres(lot):
    nets = [x["r_net"] - x["portage_r"] for x in lot]
    cumul = pic = dd = 0.0
    for r in nets:
        cumul += r
        pic = max(pic, cumul)
        dd = max(dd, pic - cumul)
    return {"n": len(nets), "total": sum(nets),
            "gagnants_pct": 100 * sum(r > 0 for r in nets) / len(nets) if nets else None,
            "dd": dd, "moyenne": sum(nets) / len(nets) if nets else None}


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    m = analyse.charger_marche(dossier, cache, "GOLD")[0]
    fin_ms = m.t[-1] + MIN_MS
    periodes = (("8 mois", analyse.DEBUT_MS, analyse.COUPURE_MS),
                ("3 mois", analyse.COUPURE_MS, V24_MS),
                ("15 jours", V24_MS, fin_ms))
    res = {"periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)], "fidele": {}, "tous": {}, "verdict": {}}
    print(f"=== OR SEUL, GESTION DU STOP ({res['periode'][0]} -> {res['periode'][1]}), garde-fou du moteur, "
          "net de frais et de portage, en R (1 R = environ 1 000 $)")
    for regle in REGLES:
        tr, refus = fidele(m, analyse.DEBUT_MS, regle)
        CS.ajouter_portage(tr)
        r = {nom: chiffres([x for x in tr if a <= x["t_entree"] < b]) for nom, a, b in periodes}
        r["sorties"] = dict(Counter(x["sortie"] for x in tr).most_common(6))
        res["fidele"][regle] = r
        print(f"{regle:8} " + " | ".join(
            f"{nom} : {r[nom]['total']:+6.1f} R, {r[nom]['n']:3} trades, {r[nom]['gagnants_pct']:4.1f} % gagnants, "
            f"DD {r[nom]['dd']:4.1f} R" for nom, _, _ in periodes))
    print()
    base = tous(m, analyse.DEBUT_MS, "tenir")
    CS.ajouter_portage(base)
    for regle in REGLES[1:]:
        t2 = tous(m, analyse.DEBUT_MS, regle)
        CS.ajouter_portage(t2)
        res["tous"][regle] = {}
        for nom, a, b in periodes:
            e = CS.ecart_par_semaine([p for p in CS.apparier(base, t2) if a <= p[0] < b], 3)
            res["tous"][regle][nom] = e
            print(f"   signal par signal, {regle:8} - tenir, {nom:8} : {e['ecart']:+.3f} R par signal "
                  f"({e['n']} signaux, p {e['p']:.3f})")
        f = res["fidele"]
        res["verdict"][regle] = all(
            f[regle][p]["total"] > f["tenir"][p]["total"] and res["tous"][regle][p]["ecart"] > 0
            for p in ("8 mois", "3 mois"))
    print("\nREGLE (mieux que tenir sur 8 mois ET sur 3 mois, en total et signal par signal) : " + ", ".join(
        f"{k} {'BAT TENIR' if v else 'ne bat pas tenir'}" for k, v in res["verdict"].items()))
    for regle in REGLES:
        print(f"   sorties {regle} : {res['fidele'][regle]['sorties']}")
    with open(os.path.join(sortie, "gestion_or.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

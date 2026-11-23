"""Rejoue la table structurelle (structure_v1.py) sur les bougies Binance 1 min.
Aucun ordre, moteur non modifie.

Usage (python du venv du moteur) :
  .venv\\Scripts\\python -I tester_structure.py <donnees> <cache> <resultats> [v1|v2]

  v1 : declencheur momentum du moteur, order block 5 min, plancher de cout du moteur.
  v2 : declencheur = cassure de structure 15 min, order block 15 min, plancher de
       cout calcule avec les vrais frais du marche. Changements decides d'apres
       l'entonnoir de la v1 (nombre de trades), pas d'apres ses gains.
"""
import csv
import json
import os
import sys
from collections import defaultdict

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import structure_v1 as S  # noqa: E402
from donnees import date_utc  # noqa: E402


def significatif(trades):
    """Esperance par trade et son p (erreur type par semaine), apres frais,
    puis portage compris."""
    paires = [(x["t_entree"], x["r_brut"], x["r_net"], x["r_net"] - x["portage_r"]) for x in trades]
    return {"net": CS.ecart_par_semaine(paires, 2), "net_portage": CS.ecart_par_semaine(paires, 3)}


def bloc(trades, jours):
    s = CS.resume(trades, jours)
    if s.get("n"):
        s["rr_moyen_vise"] = analyse.moyenne([x["rr"] for x in trades])
        s["test"] = significatif(trades)
    return s


def imprimer(titre, s):
    CS.imprimer(titre, s)
    if s.get("n"):
        t = s["test"]["net"]
        print(f"{'':24} objectif vise en moyenne a {s['rr_moyen_vise']:.1f} R ; esperance nette "
              f"{t['ecart']:+.3f} R, p = {t['p']:.3f} (hasard = 0)")


VERSIONS = {
    "v1": S.Reglages(),
    "v2": S.Reglages(declencheur="cassure 15 min", ob_min=15, fenetre_min=120,
                     plancher_frais_reels=True),
}


def main(dossier, cache, sortie, version="v1"):
    os.makedirs(sortie, exist_ok=True)
    R = VERSIONS[version]
    terrains = []
    for sym in analyse.SYMBOLES:
        m, b, _ = analyse.charger_marche(dossier, cache, sym)
        terrains.append(S.Terrain(m, b, R))
    fin_ms = max(t.m.t[-1] for t in terrains) + S.MIN_MS
    jours = (fin_ms - analyse.DEBUT_MS) / 86_400_000
    j_app = (analyse.COUPURE_MS - analyse.DEBUT_MS) / 86_400_000
    j_conf = (fin_ms - analyse.COUPURE_MS) / 86_400_000

    trades, entonnoir = S.simuler(terrains, analyse.DEBUT_MS, R)
    CS.ajouter_portage(trades)
    app = [x for x in trades if x["t_entree"] < analyse.COUPURE_MS]
    conf = [x for x in trades if x["t_entree"] >= analyse.COUPURE_MS]

    res = {"reglages": R.__dict__, "periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)],
           "entonnoir": dict(entonnoir), "tous": bloc(trades, jours),
           "8 mois": bloc(app, j_app), "3 mois": bloc(conf, j_conf)}
    print(f"=== TABLE STRUCTURELLE {version} ({res['periode'][0]} -> {res['periode'][1]}, {jours:.0f} jours)")
    imprimer("BTC + GOLD", res["tous"])
    imprimer("8 premiers mois", res["8 mois"])
    imprimer("3 derniers mois", res["3 mois"])
    for sym in analyse.SYMBOLES:
        for nom, lot, j in (("", trades, jours), (" 8 mois", app, j_app), (" 3 mois", conf, j_conf)):
            res[sym + nom] = bloc([x for x in lot if x["sym"] == sym], j)
            imprimer(sym + nom, res[sym + nom])
    print("\nPar session (heure de New York, a l'entree) :")
    for se in ("Asie", "Londres", "New York", "hors session", "week-end"):
        lot = [x for x in trades if x["session"] == se]
        res["session " + se] = bloc(lot, jours)
        if lot:
            s = res["session " + se]
            print(f"   {se:13} {s['n']:4} trades  reussite {s['reussite']:.0f} %  net {s['esp_net']:+.3f} R"
                  f"  (p {s['test']['net']['p']:.2f})  total {s['total_r']:+.1f} R")
    print("\nPar type d'objectif :")
    par_cible = defaultdict(list)
    for x in trades:
        par_cible[x["cible"]].append(x)
    for c, lot in sorted(par_cible.items(), key=lambda kv: -len(kv[1])):
        s = CS.resume(lot, jours)
        print(f"   {c:20} {s['n']:4} trades  reussite {s['reussite']:.0f} %  net {s['esp_net']:+.3f} R")
    print("\nEntonnoir : " + ", ".join(f"{k} {v}" for k, v in entonnoir.most_common()))
    res["par_mois"] = analyse.par_mois(trades)
    print("Par mois (net) : " + "  ".join(f"{k[2:]} {v['esp_net']:+.2f}R/{v['n']}"
                                         for k, v in res["par_mois"].items()))

    with open(os.path.join(sortie, f"structure_{version}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(sortie, f"structure_{version}_trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sym", "signal_utc", "entree_utc", "sortie_utc", "sens", "session", "entree", "stop",
                    "objectif", "cible", "rr_vise", "risque_bps", "sortie", "prix_sortie", "r_brut",
                    "frais_r", "r_net", "portage_r", "duree_min"])
        for x in trades:
            w.writerow([x["sym"], date_utc(x["t_signal"]), date_utc(x["t_entree"]), date_utc(x["t_sortie"]),
                        "long" if x["sens"] == 1 else "short", x["session"], x["entree"],
                        round(x["stop"], 4), x["objectif"], x["cible"], round(x["rr"], 2),
                        round(x["risque_bps"], 1), x["sortie"], x["prix_sortie"], round(x["r_brut"], 4),
                        round(x["frais_r"], 4), round(x["r_net"], 4), round(x["portage_r"], 4),
                        round(x["duree_min"], 1)])


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

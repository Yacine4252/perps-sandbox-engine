"""Compare deux facons de sortir, a entrees identiques (meme signal, meme garde-fou) :
  v2.4  : sortie quand le signal retombe ou s'inverse, sinon stop / objectif ;
  avant : stop ou objectif seulement. C'est ce que le moteur faisait reellement
          avant la v2.4 : chaque sortie sur signal echouait (HTTP 400
          invalid_position_close) et la position restait jusqu'au stop ou a
          l'objectif.

Usage (python du venv du moteur) :
  .venv\\Scripts\\python -I comparer_sorties.py <dossier_donnees> <dossier_cache> <dossier_resultats>

Le portage facture par la plateforme (toujours paye, quel que soit le sens) est
compte a part, car les positions tenues jusqu'au stop ou a l'objectif durent bien
plus longtemps. Mesure sur les 21 trades fermes du sandbox (23 oct - 6 nov 2026) :
0,0276 % par jour du notionnel pour le BTC, 0,0153 % pour l'or.
"""
import json
import math
import os
import sys
from collections import defaultdict

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import rejeu  # noqa: E402
from donnees import date_utc  # noqa: E402

PORTAGE_JOUR = {"BTC": 0.000276, "GOLD": 0.000153}
VERSIONS = {"v2.4": True, "avant v2.4": False}
SEMAINE_MS = 7 * 86_400_000


def ajouter_portage(trades):
    for x in trades:
        x["portage_r"] = PORTAGE_JOUR[x["sym"]] * x["duree_min"] / 1440 * x["entree"] / x["risque"]


def resume(trades, jours):
    s = analyse.resume_trades(trades, jours)
    if s.get("n"):
        s["portage_r"] = analyse.moyenne([x["portage_r"] for x in trades])
        s["esp_net_portage"] = s["esp_net"] - s["portage_r"]
        s["r_par_mois"] = s["total_r"] / jours * 30.44
        s["duree_moyenne_min"] = analyse.moyenne([x["duree_min"] for x in trades])
    return s


def apparier(a, b):
    """Ecart (b - a) signal par signal, apparie sur (marche, instant d'entree) :
    (instant, ecart brut, ecart net, ecart net portage compris)."""
    pa = {(x["sym"], x["t_entree"]): x for x in a}
    out = []
    for x in b:
        y = pa.get((x["sym"], x["t_entree"]))
        if y is not None:
            out.append((x["t_entree"], x["r_brut"] - y["r_brut"], x["r_net"] - y["r_net"],
                        (x["r_net"] - x["portage_r"]) - (y["r_net"] - y["portage_r"])))
    return out


def ecart_par_semaine(paires, k):
    """Ecart moyen par signal, et son p : erreur type par grappes d'une semaine,
    car des signaux proches dans le temps ne sont pas independants."""
    n = len(paires)
    if n == 0:
        return None
    m = sum(p[k] for p in paires) / n
    somme, nb = defaultdict(float), defaultdict(int)
    for p in paires:
        w = (p[0] - analyse.DEBUT_MS) // SEMAINE_MS
        somme[w] += p[k]
        nb[w] += 1
    g = len(somme)
    var = sum((somme[w] - nb[w] * m) ** 2 for w in somme)
    se = math.sqrt(var * g / (g - 1)) / n if g > 1 else float("nan")
    t = m / se if se > 0 else float("nan")
    return {"n": n, "ecart": m, "t": t, "p": math.erfc(abs(t) / math.sqrt(2)), "semaines": g}


def imprimer(titre, s):
    analyse.imprimer_resume(titre, s)
    if s.get("n"):
        print(f"{'':24} portage {s['portage_r']:.3f} R -> net portage compris {s['esp_net_portage']:+.3f} R ; "
              f"{s['r_par_mois']:+.1f} R par mois ; duree moyenne {s['duree_moyenne_min']:.0f} min")


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    marches = {sym: analyse.charger_marche(dossier, cache, sym)[0] for sym in analyse.SYMBOLES}
    fin_ms = max(m.t[-1] for m in marches.values()) + rejeu.MIN_MS
    jours = (fin_ms - analyse.DEBUT_MS) / 86_400_000
    j_app = (analyse.COUPURE_MS - analyse.DEBUT_MS) / 86_400_000
    j_conf = (fin_ms - analyse.COUPURE_MS) / 86_400_000
    app = lambda x: x["t_entree"] < analyse.COUPURE_MS        # noqa: E731

    res = {"periode": [date_utc(analyse.DEBUT_MS), date_utc(fin_ms)], "portage_jour": PORTAGE_JOUR,
           "fidele": {}, "tous": {}}
    fid, tous = {}, {}
    for nom, sortie_signal in VERSIONS.items():
        tr, refus, tentatives = rejeu.fidele(list(marches.values()), analyse.DEBUT_MS, sortie_signal)
        ajouter_portage(tr)
        fid[nom] = tr
        tous[nom] = {}
        for sym, m in marches.items():
            t2 = rejeu.tous_les_signaux(m, analyse.DEBUT_MS, sortie_signal)
            ajouter_portage(t2)
            tous[nom][sym] = t2
        r = {"tous": resume(tr, jours), "8 mois": resume([x for x in tr if app(x)], j_app),
             "3 mois": resume([x for x in tr if not app(x)], j_conf),
             "refus": dict(refus), "tentatives": tentatives, "par_mois": analyse.par_mois(tr)}
        for sym in marches:
            r[sym] = resume([x for x in tr if x["sym"] == sym], jours)
            r[sym + " 8 mois"] = resume([x for x in tr if x["sym"] == sym and app(x)], j_app)
            r[sym + " 3 mois"] = resume([x for x in tr if x["sym"] == sym and not app(x)], j_conf)
        res["fidele"][nom] = r

    print(f"=== REJEU FIDELE ({res['periode'][0]} -> {res['periode'][1]}, {jours:.0f} jours) : "
          f"meme signal, meme garde-fou, seule la sortie change")
    for cle in ("tous", "BTC", "GOLD", "8 mois", "3 mois", "BTC 8 mois", "BTC 3 mois",
                "GOLD 8 mois", "GOLD 3 mois"):
        for nom in VERSIONS:
            imprimer(f"{cle} | {nom}", res["fidele"][nom][cle])
        print()
    for nom in VERSIONS:
        r = res["fidele"][nom]
        print(f"{nom}: {r['tentatives']} tentatives, refus : "
              + ", ".join(f"{k} {v}" for k, v in sorted(r["refus"].items(), key=lambda kv: -kv[1])))
    mois_a = res["fidele"]["v2.4"]["par_mois"]
    mois_b = res["fidele"]["avant v2.4"]["par_mois"]
    mieux = [k for k in mois_b if k in mois_a and mois_b[k]["total"] > mois_a[k]["total"]]
    print(f"Mois ou 'avant v2.4' fait mieux (total net) : {len(mieux)} sur {len(mois_b)}")
    print("Par mois, total net v2.4 / avant : " + "  ".join(
        f"{k[2:]} {mois_a.get(k, {}).get('total', 0):+.1f}/{mois_b[k]['total']:+.1f}" for k in mois_b))
    res["mois_avant_meilleur"] = mieux

    print("\n=== TOUS LES SIGNAUX, a entrees identiques (ecart = avant v2.4 - v2.4, en R par signal)")
    for sym in marches:
        a, b = tous["v2.4"][sym], tous["avant v2.4"][sym]
        res["tous"][sym] = {"v2.4": resume(a, jours), "avant v2.4": resume(b, jours)}
        imprimer(f"{sym} | v2.4", res["tous"][sym]["v2.4"])
        imprimer(f"{sym} | avant v2.4", res["tous"][sym]["avant v2.4"])
        for per, filtre in (("11 mois", lambda x: True), ("8 mois", app), ("3 mois", lambda x: not app(x))):
            paires = [p for p in apparier(a, b) if filtre({"t_entree": p[0]})]
            e = {k: ecart_par_semaine(paires, i) for i, k in ((1, "brut"), (2, "net"), (3, "net_portage"))}
            res["tous"][sym][per] = e
            print(f"   {per:8} {e['net']['n']:5} signaux  ecart brut {e['brut']['ecart']:+.3f} R (p {e['brut']['p']:.3f})"
                  f"  net {e['net']['ecart']:+.3f} R (p {e['net']['p']:.3f})"
                  f"  net portage compris {e['net_portage']['ecart']:+.3f} R (p {e['net_portage']['p']:.3f})")
        print()

    with open(os.path.join(sortie, "comparaison_sorties.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    _ecrire(os.path.join(sortie, "rejeu_fidele_avant_v24_trades.csv"), fid["avant v2.4"])


def _ecrire(chemin, trades):
    import csv
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sym", "entree_utc", "sortie_utc", "sens", "entree", "stop", "objectif", "risque_bps",
                    "z", "sortie", "prix_sortie", "r_brut", "frais_r", "r_net", "portage_r", "duree_min"])
        for x in trades:
            w.writerow([x["sym"], date_utc(x["t_entree"]), date_utc(x["t_sortie"]),
                        "long" if x["sens"] == 1 else "short", x["entree"], x["stop"], x["objectif"],
                        round(x["risque_bps"], 1), x["z"], x["sortie"], x["prix_sortie"],
                        round(x["r_brut"], 4), round(x["frais_r"], 4), round(x["r_net"], 4),
                        round(x["portage_r"], 4), round(x["duree_min"], 1)])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])

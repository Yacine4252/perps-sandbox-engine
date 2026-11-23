"""Cherche la meilleure equipe de sieges avec tout ce qui a ete construit.
Aucun ordre, moteur non modifie.

Usage (python du venv du moteur) :
  .venv\\Scripts\\python -I combiner.py <dossier_donnees> <dossier_cache> <dossier_resultats>

Ce qui est essaye : 3 bases x 2 marches x (2 regles x 127 equipes + sans filtre)
= 1 530 combinaisons.
  bases    momentum v2.4 (sortie sur signal), momentum stop/objectif (comme avant
           la v2.4), table structurelle v2 ; chaque signal ou plan compte comme un
           trade independant.
  sieges   tendance 4 h, structure H1, volatilite, liquidite, delta/CVD,
           absorption, funding (definitions de sieges.py, sens fixe a l'avance).
  regles   veto : on garde le trade si aucun siege de l'equipe ne vote contre ;
           unanime : on le garde si tous les sieges de l'equipe votent pour.

Methode :
  1. Recherche sur les 8 premiers mois seulement. Critere : le t de l'esperance
     nette apres frais (gain moyen divise par son erreur type), 30 trades au moins.
     Le t recompense un gain regulier sur beaucoup de trades plutot qu'un coup de
     chance sur quelques trades.
  2. La meilleure combinaison est jugee sur les 3 derniers mois, sans rien changer.
  3. Test du hasard pour la recherche elle-meme : les votes sont decales dans le
     temps et TOUTE la recherche est refaite 200 fois. p = part des recherches au
     hasard dont la meilleure combinaison fait aussi bien que la vraie.
  4. La combinaison retenue est rejouee avec le garde-fou du moteur, sur son marche.
"""
import csv
import json
import math
import os
import random
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import rejeu  # noqa: E402
import structure_v1 as S  # noqa: E402
from donnees import date_utc  # noqa: E402
from sieges import CONTRE, CONTRE_SEUL, NEUTRE, POUR, Contexte  # noqa: E402

SIEGES7 = ["tendance_4h", "structure_h1", "volatilite", "liquidite",
           "of_delta_cvd", "of_absorption", "of_funding"]
NOMS = {"tendance_4h": "tendance 4 h", "structure_h1": "structure H1", "volatilite": "volatilite",
        "liquidite": "liquidite", "of_delta_cvd": "delta/CVD", "of_absorption": "absorption",
        "of_funding": "funding"}
BASES = ["momentum v2.4", "momentum stop/objectif", "structure v2"]
REGLES = ["veto", "unanime"]
CONFIGS = [("sans filtre", 0)] + [(r, e) for r in REGLES for e in range(1, 2 ** len(SIEGES7))]
N_MIN = 30
N_HASARD = 200
R_STRUCT = S.Reglages(declencheur="cassure 15 min", ob_min=15, fenetre_min=120,
                      plancher_frais_reels=True)


def masques(votes):
    """(sieges POUR, sieges CONTRE) en bits. Absorption et funding ne votent que
    contre : chez eux, 'pas d'objection' compte pour."""
    pour = contre = 0
    for k, s in enumerate(SIEGES7):
        v = votes.get(s)
        if s in CONTRE_SEUL and v == NEUTRE:
            v = POUR
        if v == POUR:
            pour |= 1 << k
        elif v == CONTRE:
            contre |= 1 << k
    return pour, contre


def garde(regle, equipe, pour, contre):
    if equipe == 0:
        return True
    if regle == "veto":
        return contre & equipe == 0
    return pour & equipe == equipe


def nom_config(cfg):
    regle, equipe = cfg
    if equipe == 0:
        return "sans filtre"
    return f"{regle} : " + " + ".join(NOMS[s] for k, s in enumerate(SIEGES7) if equipe >> k & 1)


class Lot:
    """Trades d'une base sur un marche et une periode, groupes par votes."""

    def __init__(self, trades):
        self.trades = trades
        sig = [(x["pour"], x["contre"]) for x in trades]
        self.r = [x["r_net"] for x in trades]
        self.signatures = sorted(set(sig))
        idx = {s: k for k, s in enumerate(self.signatures)}
        self.k = [idx[s] for s in sig]
        self.passe = {cfg: [k for k, (p, c) in enumerate(self.signatures) if garde(cfg[0], cfg[1], p, c)]
                      for cfg in CONFIGS}

    def evaluer(self, decalage=0):
        """{config: (n, esperance, t)}, les votes decales de `decalage` trades."""
        n = len(self.r)
        agg = [[0, 0.0, 0.0] for _ in self.signatures]
        for j in range(n):
            a = agg[self.k[(j + decalage) % n]]
            r = self.r[j]
            a[0] += 1
            a[1] += r
            a[2] += r * r
        out = {}
        for cfg, ks in self.passe.items():
            nn, s, ss = 0, 0.0, 0.0
            for k in ks:
                a = agg[k]
                nn += a[0]
                s += a[1]
                ss += a[2]
            if nn >= 2:
                m = s / nn
                var = max(ss - nn * m * m, 0.0) / (nn - 1)
                out[cfg] = (nn, m, m / math.sqrt(var / nn) if var > 1e-12 else 0.0)
            else:
                out[cfg] = (nn, None, None)
        return out


def p_unilaterale(trades):
    """p de 'esperance nette > 0', erreur type par semaine."""
    if len(trades) < 2:
        return None
    e = CS.ecart_par_semaine([(x["t_entree"], x["r_brut"], x["r_net"], 0.0) for x in trades], 2)
    if e is None or e["p"] != e["p"]:
        return None
    return e["p"] / 2 if e["ecart"] > 0 else 1 - e["p"] / 2


def filtrer(trades, cfg):
    return [x for x in trades if garde(cfg[0], cfg[1], x["pour"], x["contre"])]


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    marches, ctx, terr, lots = {}, {}, {}, {}
    for sym in analyse.SYMBOLES:
        m, b, fund = analyse.charger_marche(dossier, cache, sym)
        marches[sym], ctx[sym] = m, Contexte(b, fund)
        terr[sym] = S.Terrain(m, b, R_STRUCT, ctx[sym])
        votes = {}

        def voter(i, d, entree, objectif, sym=sym, votes=votes):
            cle = (i, d, entree, objectif)
            if cle not in votes:
                votes[cle] = masques(ctx[sym].voter(i, d, entree, objectif)[0])
            return votes[cle]

        bases = {"momentum v2.4": (rejeu.tous_les_signaux(m, analyse.DEBUT_MS, True), "i_entree"),
                 "momentum stop/objectif": (rejeu.tous_les_signaux(m, analyse.DEBUT_MS, False), "i_entree"),
                 "structure v2": (S.simuler_independant(terr[sym], analyse.DEBUT_MS, R_STRUCT)[0], "i_signal")}
        for base, (tr, cle_i) in bases.items():
            for x in tr:
                x["pour"], x["contre"] = voter(x[cle_i], x["sens"], x["entree"], x["objectif"])
            CS.ajouter_portage(tr)
            lots[(base, sym)] = tr
        print(f"{sym} : " + ", ".join(f"{base} {len(lots[(base, sym)])} trades" for base in BASES), flush=True)

    coupe = analyse.COUPURE_MS
    app = {k: [x for x in v if x["t_entree"] < coupe] for k, v in lots.items()}
    conf = {k: [x for x in v if x["t_entree"] >= coupe] for k, v in lots.items()}
    lots_app = {k: Lot(v) for k, v in app.items()}
    ev_app = {k: L.evaluer() for k, L in lots_app.items()}
    ev_conf = {k: Lot(v).evaluer() for k, v in conf.items()}

    candidats = [(k, cfg) for k, ev in ev_app.items() for cfg, (n, m, t) in ev.items()
                 if n >= N_MIN and t is not None]
    classement = sorted(candidats, key=lambda kc: -ev_app[kc[0]][kc[1]][2])
    print(f"\n{len(CONFIGS) * len(lots)} combinaisons, {len(candidats)} avec au moins {N_MIN} trades sur 8 mois")

    def ligne(k, cfg):
        n, m, t = ev_app[k][cfg]
        lc = filtrer(conf[k], cfg)
        mc = analyse.moyenne([x["r_net"] for x in lc])
        pc = p_unilaterale(lc)
        return {"base": k[0], "marche": k[1], "combinaison": nom_config(cfg),
                "8 mois": {"n": n, "esp_net": m, "t": t},
                "3 mois": {"n": len(lc), "esp_net": mc, "p_unilaterale": pc,
                           "total": sum(x["r_net"] for x in lc)}}

    def imprimer(L):
        a, c = L["8 mois"], L["3 mois"]
        print(f"   {L['base']:22} {L['marche']:4} {L['combinaison'][:58]:58} | 8 mois {a['n']:4} trades "
              f"{a['esp_net']:+.3f} R t {a['t']:+.2f} | 3 mois {c['n']:4} trades "
              f"{c['esp_net'] if c['esp_net'] is None else round(c['esp_net'], 3):>7} R "
              f"p {c['p_unilaterale'] if c['p_unilaterale'] is None else round(c['p_unilaterale'], 3)}")

    res = {"n_combinaisons": len(CONFIGS) * len(lots), "n_candidats": len(candidats)}
    print("\nSans filtre (rappel) :")
    res["sans filtre"] = [ligne(k, ("sans filtre", 0)) for k in lots]
    for L in res["sans filtre"]:
        imprimer(L)
    print("\nLes 10 meilleures sur 8 mois, et ce qu'elles font sur les 3 mois suivants :")
    res["top10"] = [ligne(k, cfg) for k, cfg in classement[:10]]
    for L in res["top10"]:
        imprimer(L)
    print("\nLa meilleure pour chaque base et chaque marche :")
    res["par_base"] = []
    for k in lots:
        meilleurs = [kc for kc in classement if kc[0] == k]
        if meilleurs:
            res["par_base"].append(ligne(*meilleurs[0]))
            imprimer(res["par_base"][-1])

    # test du hasard pour la recherche entiere
    (k_best, cfg_best) = classement[0]
    t_obs = ev_app[k_best][cfg_best][2]
    rnd = random.Random(2026)
    nuls = []
    for _ in range(N_HASARD):
        mx = float("-inf")
        for k, L in lots_app.items():
            n = len(L.r)
            if n < N_MIN:
                continue
            mm = max(1, n // 20)
            for cfg, (nn, m, t) in L.evaluer(rnd.randint(mm, n - mm)).items():
                if nn >= N_MIN and t is not None and t > mx:
                    mx = t
        nuls.append(mx)
    nuls.sort()
    p_recherche = (1 + sum(1 for x in nuls if x >= t_obs)) / (1 + len(nuls))
    res["hasard"] = {"t_meilleure": t_obs, "p_recherche": p_recherche,
                     "t_hasard_median": nuls[len(nuls) // 2], "t_hasard_95": nuls[int(0.95 * len(nuls))]}
    print(f"\nTest du hasard sur la recherche entiere : meilleure t = {t_obs:.2f} ; au hasard, la "
          f"meilleure combinaison atteint t = {res['hasard']['t_hasard_median']:.2f} en mediane et "
          f"{res['hasard']['t_hasard_95']:.2f} une fois sur 20 ; p = {p_recherche:.3f}")

    # la meilleure, rejouee avec le garde-fou du moteur sur son marche
    base, sym = k_best

    def filtre(s, i, d, entree, objectif):
        p, c = masques(ctx[s].voter(i, d, entree, objectif)[0])
        return not garde(cfg_best[0], cfg_best[1], p, c)

    rejeux = {}
    for nom, f in (("sans filtre", None), ("avec la combinaison", filtre)):
        if base == "structure v2":
            tr, _ = S.simuler([terr[sym]], analyse.DEBUT_MS, R_STRUCT, f)
        else:
            tr, _, _ = rejeu.fidele([marches[sym]], analyse.DEBUT_MS, base == "momentum v2.4", f)
        CS.ajouter_portage(tr)
        fin = marches[sym].t[-1] + rejeu.MIN_MS
        rejeux[nom] = {
            "8 mois": CS.resume([x for x in tr if x["t_entree"] < coupe], (coupe - analyse.DEBUT_MS) / 864e5),
            "3 mois": CS.resume([x for x in tr if x["t_entree"] >= coupe], (fin - coupe) / 864e5)}
        rejeux[nom]["3 mois"]["p_unilaterale"] = p_unilaterale([x for x in tr if x["t_entree"] >= coupe])
    res["rejeu_garde_fou"] = {"base": base, "marche": sym, "combinaison": nom_config(cfg_best), **rejeux}
    print(f"\nRejeu avec le garde-fou du moteur : {base}, {sym}, {nom_config(cfg_best)}")
    for nom, r in rejeux.items():
        for per in ("8 mois", "3 mois"):
            s = r[per]
            if s.get("n"):
                print(f"   {nom:20} {per} : {s['n']:4} trades ({s['par_jour']:.2f}/j)  net {s['esp_net']:+.3f} R"
                      f"  total {s['total_r']:+.1f} R  DD {s['dd_max']:.1f} R  reussite {s['reussite']:.0f} %")

    with open(os.path.join(sortie, "combinaisons.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["base", "marche", "combinaison", "n_8mois", "esp_8mois", "t_8mois", "n_3mois", "esp_3mois", "t_3mois"])
        for k, ev in ev_app.items():
            for cfg, (n, m, t) in ev.items():
                nc, mc, tc = ev_conf[k][cfg]
                w.writerow([k[0], k[1], nom_config(cfg), n, m if m is None else round(m, 4),
                            t if t is None else round(t, 2), nc, mc if mc is None else round(mc, 4),
                            tc if tc is None else round(tc, 2)])
    with open(os.path.join(sortie, "combiner.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])

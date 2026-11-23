"""Rejeu du momentum v2.4 sur 330 jours de bougies Binance 1 min, et vote des
sieges sur chaque signal. Aucun ordre, aucune modification du moteur.

Usage (python du venv du moteur) :
  .venv\\Scripts\\python -I analyse.py <dossier_donnees> <dossier_cache> <dossier_resultats>
Il faut d'abord signaux_moteur.py (le signal calcule par le code du moteur).

Methode :
  - periode d'apprentissage : du 11 dec 2025 au 10 aout 2026 (8 mois) ;
    confirmation : du 11 aout au 6 nov 2026 (3 mois), sans rien changer ;
  - pour chaque siege et chaque marche : esperance en R des trades POUR et CONTRE,
    avant et apres frais ;
  - "mieux que le hasard" : on decale les votes dans le temps (decalage circulaire)
    des centaines de fois. Cela garde leur rythme mais casse le lien avec le
    resultat des trades. p = part des decalages qui font aussi bien que le vrai vote ;
  - un siege est retenu sur les 8 mois si p < 5 % (dans un sens ou dans l'autre),
    puis confirme sur les 3 mois si l'ecart garde le meme sens avec p < 5 %.
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import rejeu  # noqa: E402
from donnees import MIN_MS, charger_1m, charger_funding, date_utc  # noqa: E402
from sieges import CONTRE, CONTRE_SEUL, NEUTRE, POUR, SIEGES, Contexte  # noqa: E402

SYMBOLES = {"BTC": "BTCUSDT", "GOLD": "XAUUSDT"}       # ordre du moteur : BTC puis GOLD
FRAIS_BPS = {"BTC": 3.0, "GOLD": 0.5}                  # factures par la plateforme, par cote
DEBUT_MS = int(datetime(2025, 12, 11, tzinfo=timezone.utc).timestamp() * 1000)
COUPURE_MS = int(datetime(2026, 8, 11, tzinfo=timezone.utc).timestamp() * 1000)
SEUIL = 0.05
N_MIN = 30          # trades minimum dans chaque groupe pour juger un siege


# --------------------------------------------------------------------------
# chargement
# --------------------------------------------------------------------------

def charger_marche(dossier, cache, sym):
    b = charger_1m(os.path.join(dossier, f"{SYMBOLES[sym]}_1m.csv"))
    d, z, dist, vu = [], [], [], []
    with open(os.path.join(cache, f"{SYMBOLES[sym]}_signaux.csv"), newline="") as f:
        r = csv.reader(f)
        next(r)
        for k, x in enumerate(r):
            assert int(x[0]) == b.t[k], "cache de signaux decale"
            d.append(int(x[1]))
            z.append(float(x[2]) if x[2] else None)
            dist.append(float(x[3]) if x[3] else None)
            vu.append(x[4] == "1")
    assert len(d) == len(b.t)
    m = rejeu.Marche(sym, b.t, b.o, b.h, b.l, b.c, d, dist, z, vu, FRAIS_BPS[sym])
    fund = charger_funding(os.path.join(dossier, f"{SYMBOLES[sym]}_funding.csv"))
    return m, b, fund


# --------------------------------------------------------------------------
# statistiques
# --------------------------------------------------------------------------

def moyenne(xs):
    return sum(xs) / len(xs) if xs else None


def resume_trades(trades, jours):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    net = [x["r_net"] for x in trades]
    brut = [x["r_brut"] for x in trades]
    gains = sum(r for r in net if r > 0)
    pertes = -sum(r for r in net if r < 0)
    cumul = pic = dd = 0.0
    serie = serie_max = 0
    for r in net:
        cumul += r
        pic = max(pic, cumul)
        dd = max(dd, pic - cumul)
        serie = serie + 1 if r < 0 else 0
        serie_max = max(serie_max, serie)
    sorties = Counter(x["sortie"] for x in trades)
    return {
        "n": n, "par_jour": n / jours,
        "esp_brut": moyenne(brut), "esp_net": moyenne(net),
        "frais_r": moyenne([x["frais_r"] for x in trades]),
        "reussite": 100.0 * sum(r > 0 for r in net) / n,
        "pf": gains / pertes if pertes else None,
        "total_r": sum(net), "dd_max": dd, "pertes_consecutives": serie_max,
        "sorties_pct": {k: 100.0 * v / n for k, v in sorties.items()},
        "ambigus": sum(1 for x in trades if x["ambigu"]),
        "duree_mediane_min": sorted(x["duree_min"] for x in trades)[n // 2],
        "risque_median_bps": sorted(x["risque_bps"] for x in trades)[n // 2],
    }


def par_mois(trades):
    m = defaultdict(list)
    for x in trades:
        m[date_utc(x["t_entree"])[:7]].append(x["r_net"])
    return {k: {"n": len(v), "esp_net": sum(v) / len(v), "total": sum(v)} for k, v in sorted(m.items())}


def ecarts(votes, brut, net, decal=0):
    """Ecart (moyenne POUR - moyenne CONTRE), avant et apres frais, les votes
    etant decales de `decal` trades."""
    n = len(votes)
    sp = sc = bp = bc = 0.0
    np_ = nc = 0
    for j in range(n):
        v = votes[(j + decal) % n]
        if v == POUR:
            sp += net[j]
            bp += brut[j]
            np_ += 1
        elif v == CONTRE:
            sc += net[j]
            bc += brut[j]
            nc += 1
    if np_ == 0 or nc == 0:
        return None
    return bp / np_ - bc / nc, sp / np_ - sc / nc


def test_hasard(votes, brut, net, n_decalages=499):
    """Ecarts observes et p bilaterales par decalage circulaire des votes.
    Rend aussi la liste nulle pour les tests unilateraux."""
    n = len(votes)
    obs = ecarts(votes, brut, net)
    if obs is None:
        return None
    m = max(1, n // 20)       # les petits decalages laisseraient les votes presque intacts
    ks = list(range(m, n - m + 1))
    if len(ks) > n_decalages:
        pas = len(ks) / n_decalages
        ks = [ks[int(j * pas)] for j in range(n_decalages)]
    nul = [e for e in (ecarts(votes, brut, net, k) for k in ks) if e is not None]
    return obs, nul


def p_valeur(obs, nul, sens):
    """sens 0 : bilaterale ; +1 : ecart >= obs ; -1 : ecart <= obs."""
    if not nul:
        return None
    if sens == 0:
        k = sum(1 for e in nul if abs(e) >= abs(obs) - 1e-12)
    elif sens > 0:
        k = sum(1 for e in nul if e >= obs - 1e-12)
    else:
        k = sum(1 for e in nul if e <= obs + 1e-12)
    return (k + 1) / (len(nul) + 1)


def votes_compares(trades, siege):
    """Votes utilises pour la comparaison. Les sieges qui ne votent que CONTRE
    (absorption, funding) sont compares a "pas d'objection" : NEUTRE compte POUR."""
    vs = [x["votes"][siege] for x in trades]
    if siege in CONTRE_SEUL:
        vs = [POUR if v == NEUTRE else v for v in vs]
    return vs


def groupe(trades, votes, v):
    xs = [x for x, w in zip(trades, votes) if w == v]
    return {"n": len(xs), "brut": moyenne([x["r_brut"] for x in xs]),
            "net": moyenne([x["r_net"] for x in xs])}


def juger_siege(trades, siege, sens_impose=None):
    """Statistiques d'un siege sur une periode. sens_impose : le sens retenu en
    apprentissage (+1 normal, -1 inverse), pour le test unilateral."""
    votes = votes_compares(trades, siege)
    brut = [x["r_brut"] for x in trades]
    net = [x["r_net"] for x in trades]
    out = {"pour": groupe(trades, votes, POUR), "contre": groupe(trades, votes, CONTRE),
           "neutre": groupe(trades, votes, NEUTRE), "absent": groupe(trades, votes, None)}
    t = test_hasard(votes, brut, net)
    if t is None:
        return out
    (e_brut, e_net), nul = t
    nul_b = [e[0] for e in nul]
    nul_n = [e[1] for e in nul]
    out.update(ecart_brut=e_brut, ecart_net=e_net,
               p_brut=p_valeur(e_brut, nul_b, 0), p_net=p_valeur(e_net, nul_n, 0),
               n_decalages=len(nul))
    if sens_impose:
        out["p_net_unilateral"] = p_valeur(e_net, nul_n, sens_impose)
        out["p_brut_unilateral"] = p_valeur(e_brut, nul_b, sens_impose)
    return out


def selection(app, conf, sieges):
    """Retenus sur les 8 mois, puis jugement sur les 3 mois."""
    verdicts = {}
    for s in sieges:
        a = app[s]
        retenu = (a.get("p_net") is not None and a["p_net"] < SEUIL
                  and a["pour"]["n"] >= N_MIN and a["contre"]["n"] >= N_MIN)
        sens = (1 if a.get("ecart_net", 0) > 0 else -1) if retenu else None
        v = {"retenu": retenu, "sens": sens}
        if retenu:
            c = conf[s]
            ec = c.get("ecart_net")
            if ec is None:
                v["confirmation"] = "pas de donnees"
            elif ec * sens > 0 and c.get("p_net_unilateral", 1) < SEUIL:
                v["confirmation"] = "confirme"
            elif ec * sens > 0:
                v["confirmation"] = "meme sens, pas significatif"
            else:
                v["confirmation"] = "inverse"
        verdicts[s] = v
    return verdicts


def filtre_combine(trades, retenus):
    """Garde un trade si aucun siege retenu ne s'y oppose (dans son sens retenu)."""
    votes = {s: votes_compares(trades, s) for s in retenus}
    garde, veto = [], []
    for k, x in enumerate(trades):
        oppose = any(votes[s][k] == -sens for s, sens in retenus.items())
        (veto if oppose else garde).append(x)
    return garde, veto


# --------------------------------------------------------------------------
# sorties
# --------------------------------------------------------------------------

def ecrire_trades(chemin, trades):
    cols = ["sym", "entree_utc", "sortie_utc", "sens", "entree", "stop", "objectif", "risque_bps",
            "z", "sortie", "prix_sortie", "r_brut", "frais_r", "r_net", "duree_min"] + SIEGES + \
           ["structure_genre", "atr_centile", "obstacles", "cvd30", "cvd60", "funding"]
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for x in trades:
            i = x.get("info", {})
            w.writerow([x["sym"], date_utc(x["t_entree"]), date_utc(x["t_sortie"]),
                        "long" if x["sens"] == 1 else "short", x["entree"], x["stop"], x["objectif"],
                        round(x["risque_bps"], 1), x["z"], x["sortie"], x["prix_sortie"],
                        round(x["r_brut"], 4), round(x["frais_r"], 4), round(x["r_net"], 4),
                        round(x["duree_min"], 1)] + [x["votes"][s] for s in SIEGES] +
                       [i.get("structure_genre"), _r(i.get("atr_centile")), i.get("obstacles"),
                        _r(i.get("cvd30")), _r(i.get("cvd60")), i.get("funding")])


def _r(x, n=2):
    return round(x, n) if isinstance(x, float) else x


def _f(x, fmt="{:+.2f}"):
    return "-" if x is None else fmt.format(x)


def imprimer_resume(titre, s):
    if not s.get("n"):
        print(f"{titre}: aucun trade")
        return
    so = s["sorties_pct"]
    print(f"{titre:24} {s['n']:5} trades  {s['par_jour']:.2f}/j  brut {s['esp_brut']:+.3f} R  "
          f"net {s['esp_net']:+.3f} R  frais {s['frais_r']:.3f} R  reussite {s['reussite']:.1f} %  "
          f"PF {_f(s['pf'], '{:.2f}')}  total {s['total_r']:+.1f} R  DD {s['dd_max']:.1f} R  "
          f"pertes de suite {s['pertes_consecutives']}  stop {so.get('sl', 0):.0f} % / objectif "
          f"{so.get('tp', 0):.0f} % / signal {so.get('signal', 0):.0f} %  stop median "
          f"{s['risque_median_bps']:.0f} bps  duree mediane {s['duree_mediane_min']:.0f} min")


def imprimer_sieges(sym, app, conf, verdicts):
    print(f"\n--- {sym} : esperance nette (R) des trades POUR / CONTRE   [8 mois | 3 mois]"
          f"   (absorption et funding : 'pour' = pas d'objection)")
    print(f"{'siege':14} {'n pour/contre':>14} {'pour':>7} {'contre':>7} {'ecart':>7} {'p':>6} "
          f"{'brut ec.':>8} | {'n pour/contre':>14} {'pour':>7} {'contre':>7} {'ecart':>7} {'p 1c':>6}  verdict")
    for s in SIEGES:
        a, c, v = app[s], conf[s], verdicts[s]
        def bloc(x, p_cle):
            return (f"{x['pour']['n']:>6}/{x['contre']['n']:<7} {_f(x['pour']['net']):>7} "
                    f"{_f(x['contre']['net']):>7} {_f(x.get('ecart_net')):>7} "
                    f"{_f(x.get(p_cle), '{:.3f}'):>6}")
        if v["retenu"]:
            verdict = ("retenu" + (" (inverse)" if v["sens"] < 0 else "") + " -> " + v["confirmation"])
        else:
            verdict = "non retenu"
        print(f"{s:14} {bloc(a, 'p_net')} {_f(a.get('ecart_brut')):>8} | "
              f"{bloc(c, 'p_net_unilateral' if v['retenu'] else 'p_net')}  {verdict}")


def main(dossier, cache, sortie):
    os.makedirs(sortie, exist_ok=True)
    marches, ctx = {}, {}
    for sym in SYMBOLES:
        m, b, fund = charger_marche(dossier, cache, sym)
        marches[sym] = m
        ctx[sym] = Contexte(b, fund)
        print(f"{sym}: {len(b.t)} bougies 1 min du {date_utc(b.t[0])} au {date_utc(b.t[-1])}, "
              f"{len(fund[0])} fundings", flush=True)
    fin_ms = max(m.t[-1] for m in marches.values()) + MIN_MS
    jours = (fin_ms - DEBUT_MS) / 86_400_000

    # 1. rejeu fidele (garde-fou du moteur)
    fid, refus, tentatives = rejeu.fidele(list(marches.values()), DEBUT_MS)
    # 2. tous les signaux, marche par marche
    tous = {sym: rejeu.tous_les_signaux(m, DEBUT_MS) for sym, m in marches.items()}

    for x in fid + [x for v in tous.values() for x in v]:
        x["votes"], x["info"] = ctx[x["sym"]].voter(x["i_entree"], x["sens"], x["entree"], x["objectif"])

    resume = {"periode": [date_utc(DEBUT_MS), date_utc(fin_ms)], "jours": jours,
              "coupure": date_utc(COUPURE_MS), "frais_bps": FRAIS_BPS}
    print(f"\n=== REJEU FIDELE du momentum v2.4 ({resume['periode'][0]} -> {resume['periode'][1]}, "
          f"{jours:.0f} jours, garde-fou du moteur)")
    r_fid = {"tous": resume_trades(fid, jours)}
    imprimer_resume("BTC + GOLD", r_fid["tous"])
    for sym in SYMBOLES:
        r_fid[sym] = resume_trades([x for x in fid if x["sym"] == sym], jours)
        imprimer_resume(sym, r_fid[sym])
    for nom, cond in (("8 mois", lambda x: x["t_entree"] < COUPURE_MS),
                      ("3 mois", lambda x: x["t_entree"] >= COUPURE_MS)):
        sous = [x for x in fid if cond(x)]
        j = ((COUPURE_MS - DEBUT_MS) if nom == "8 mois" else (fin_ms - COUPURE_MS)) / 86_400_000
        r_fid[nom] = resume_trades(sous, j)
        imprimer_resume(f"BTC + GOLD {nom}", r_fid[nom])
    print(f"Tentatives d'entree : {tentatives}, refusees par le garde-fou : "
          + ", ".join(f"{k} {v}" for k, v in refus.most_common()))
    r_fid["refus"] = dict(refus)
    r_fid["tentatives"] = tentatives
    r_fid["par_mois"] = par_mois(fid)
    print("Par mois (net) : " + "  ".join(f"{k[2:]} {v['esp_net']:+.2f}R/{v['n']}"
                                         for k, v in r_fid["par_mois"].items()))
    resume["fidele"] = r_fid
    ecrire_trades(os.path.join(sortie, "rejeu_fidele_trades.csv"), fid)

    print(f"\n=== TOUS LES SIGNAUX (chaque signal neuf = un trade, sans garde-fou)")
    resume["tous"] = {}
    for sym, tr in tous.items():
        app = [x for x in tr if x["t_entree"] < COUPURE_MS]
        conf = [x for x in tr if x["t_entree"] >= COUPURE_MS]
        r = {"tout": resume_trades(tr, jours),
             "8 mois": resume_trades(app, (COUPURE_MS - DEBUT_MS) / 86_400_000),
             "3 mois": resume_trades(conf, (fin_ms - COUPURE_MS) / 86_400_000),
             "par_mois": par_mois(tr)}
        imprimer_resume(sym, r["tout"])
        imprimer_resume(f"{sym} 8 mois", r["8 mois"])
        imprimer_resume(f"{sym} 3 mois", r["3 mois"])
        ecrire_trades(os.path.join(sortie, f"tous_signaux_{sym}.csv"), tr)

        j_app = {s: juger_siege(app, s) for s in SIEGES}
        verdicts = selection(j_app, {s: {} for s in SIEGES}, SIEGES)
        j_conf = {s: juger_siege(conf, s, verdicts[s]["sens"]) for s in SIEGES}
        verdicts = selection(j_app, j_conf, SIEGES)
        imprimer_sieges(sym, j_app, j_conf, verdicts)

        retenus = {s: v["sens"] for s, v in verdicts.items() if v["retenu"]}
        comb = {}
        if retenus:
            for nom, lot in (("8 mois", app), ("3 mois", conf)):
                garde, veto = filtre_combine(lot, retenus)
                comb[nom] = {"tous": resume_trades(lot, 1), "gardes": resume_trades(garde, 1),
                             "vetos": resume_trades(veto, 1)}
                print(f"Filtre des sieges retenus ({', '.join(retenus)}) sur {nom} : "
                      f"{len(garde)} gardes {_f(comb[nom]['gardes'].get('esp_net'))} R net, "
                      f"{len(veto)} ecartes {_f(comb[nom]['vetos'].get('esp_net'))} R net, "
                      f"tous {_f(comb[nom]['tous'].get('esp_net'))} R net")
        r.update(sieges_8_mois=j_app, sieges_3_mois=j_conf, verdicts=verdicts, filtre=comb)
        resume["tous"][sym] = r

    # les sieges sur les trades du rejeu fidele, pour information
    resume["fidele_sieges"] = {}
    for sym in SYMBOLES:
        tr = [x for x in fid if x["sym"] == sym]
        resume["fidele_sieges"][sym] = {s: juger_siege(tr, s) for s in SIEGES}

    with open(os.path.join(sortie, "resume.json"), "w", encoding="utf-8") as f:
        json.dump(resume, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])

"""Cassure du range d'ouverture (ORB) sur l'or et le BTC, bougies 1 min (23 nov 2026).

Idee de Muse pour le trader IA : rester dehors en Asie, trader a Londres et a
New York la cassure du range d'ouverture avec confirmation. C'est une regle
mecanique : on la teste seule avant de la confier a une IA. Lecture de donnees
publiques deja telechargees (recherche/sieges/donnees) : aucun ordre, moteur,
parametres et .env non touches.

Regle testee, sans rien regler apres coup (grille fixee avant de lancer) :
  ouvertures (heure locale, changements d'heure compris) :
      Asie 18:00 New York (dim-jeu, pour comparer), Londres 08:00 Londres,
      New York 08:30 et 09:30 New York (lun-ven) ;
  range = plus haut / plus bas des N premieres minutes, N = 15, 30, 60 ;
  confirmation = premiere bougie 5 min (alignee sur l'ouverture) qui CLOTURE
      hors du range, dans les 3 h qui suivent l'ouverture ; entree au prix de
      cloture ; un seul trade par seance et par marche ;
  stop = autre cote du range, ou milieu du range ;
  objectif = 1 R, 2 R, ou aucun ; sortie forcee 6 h apres l'ouverture ;
  bornes du plan : trade refuse si le stop est a moins de 0,15 % ou plus de 3 %.
Couts : frais par cote du sandbox (BTC 3 points de base, or 0,5), plus
1 point de base de glissement par cote. Si stop et objectif sont touches dans
la meme minute, on compte le stop. Pas de filtre news : pas d'archive
historique du calendrier.
Repere : le sens inverse (vendre la cassure haute) avec les memes regles.
Resultat en R et en dollars a 0,3 % de risque sur 100 000 $ (300 $ par R).

Usage : <python du venv> -I cassure_ouverture.py <dossier_donnees> <dossier_resultats>
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")
MIN = 60_000
FRAIS_BPS = {"BTC": 3.0, "GOLD": 0.5}
GLISSEMENT_BPS = 1.0
DOLLARS_PAR_R = 300.0
SEANCES = {                      # nom : (fuseau, heure, minute, jours ISO autorises)
    "Asie 18h NY": (NY, 18, 0, (7, 1, 2, 3, 4)),
    "Londres 8h": (LDN, 8, 0, (1, 2, 3, 4, 5)),
    "NY 8h30": (NY, 8, 30, (1, 2, 3, 4, 5)),
    "NY 9h30": (NY, 9, 30, (1, 2, 3, 4, 5)),
}
RANGES = (15, 30, 60)
STOPS = ("autre_cote", "milieu")
OBJECTIFS = (1.0, 2.0, None)
FENETRE_ENTREE_MIN = 180
SORTIE_MIN = 360


def lire(chemin):
    out = {}
    with open(chemin, newline="") as f:
        r = csv.reader(f)
        next(r)
        for x in r:
            out[int(x[0])] = (float(x[1]), float(x[2]), float(x[3]), float(x[4]))
    return out


def ouvertures(barres, fuseau, h, m, jours):
    t0 = min(barres)
    t1 = max(barres)
    d = datetime.fromtimestamp(t0 / 1000, timezone.utc).astimezone(fuseau).date()
    fin = datetime.fromtimestamp(t1 / 1000, timezone.utc).astimezone(fuseau).date()
    while d <= fin:
        if d.isoweekday() in jours:
            loc = datetime(d.year, d.month, d.day, h, m, tzinfo=fuseau)
            yield int(loc.astimezone(timezone.utc).timestamp() * 1000)
        d += timedelta(days=1)


def trade(barres, sym, t_ouv, n_range, stop_mode, objectif, inverse):
    rng = [barres.get(t_ouv + i * MIN) for i in range(n_range)]
    if sum(b is None for b in rng) > n_range // 5:
        return None
    rng = [b for b in rng if b]
    haut, bas = max(b[1] for b in rng), min(b[2] for b in rng)
    if haut <= bas:
        return None
    # confirmation : premiere bougie 5 min qui cloture hors du range
    entree = sens = t_entree = None
    t = t_ouv + n_range * MIN
    while t + 5 * MIN <= t_ouv + FENETRE_ENTREE_MIN * MIN:
        b5 = [barres.get(t + i * MIN) for i in range(5)]
        derniere = next((b for b in reversed(b5) if b), None)
        if derniere:
            c = derniere[3]
            if c > haut:
                sens = 1
            elif c < bas:
                sens = -1
            if sens:
                entree, t_entree = c, t + 5 * MIN
                break
        t += 5 * MIN
    if not sens:
        return None
    if stop_mode == "autre_cote":
        stop = bas if sens == 1 else haut
    else:
        stop = (haut + bas) / 2
    if inverse:                                   # vendre la cassure haute, acheter la basse
        sens = -sens
        stop = entree + (entree - stop)
    dist = abs(entree - stop)
    if not (0.0015 <= dist / entree <= 0.03):
        return {"refuse": True}
    cible = entree + sens * objectif * dist if objectif else None
    sortie, raison, dernier = None, "temps", entree
    t = t_entree
    while t < t_ouv + SORTIE_MIN * MIN:
        b = barres.get(t)
        if b:
            o, h, l, c = b
            if (l <= stop) if sens == 1 else (h >= stop):
                sortie, raison = (min(o, stop) if sens == 1 else max(o, stop)), "stop"
                break
            if cible is not None and ((h >= cible) if sens == 1 else (l <= cible)):
                sortie, raison = cible, "objectif"
                break
            dernier = c
        t += MIN
    if sortie is None:
        sortie = dernier
    cout = (FRAIS_BPS[sym] + GLISSEMENT_BPS) / 10_000 * (entree + sortie)
    r = (sens * (sortie - entree) - cout) / dist
    return {"t": t_entree, "sens": sens, "r": r, "raison": raison, "dist_pct": 100 * dist / entree}


def stats(rs):
    if not rs:
        return {"n": 0}
    gains = sum(x for x in rs if x > 0)
    pertes = -sum(x for x in rs if x < 0)
    cum = pic = dd = 0.0
    for x in rs:
        cum += x
        pic = max(pic, cum)
        dd = max(dd, pic - cum)
    return {"n": len(rs), "esperance": sum(rs) / len(rs), "total": sum(rs),
            "gagnants_pct": 100 * sum(x > 0 for x in rs) / len(rs),
            "pf": gains / pertes if pertes else None, "dd": dd}


def main(dossier, sortie):
    os.makedirs(sortie, exist_ok=True)
    marches = {"GOLD": lire(os.path.join(dossier, "XAUUSDT_1m.csv")),
               "BTC": lire(os.path.join(dossier, "BTCUSDT_1m.csv"))}
    res, lignes = {}, []
    for sym, barres in marches.items():
        t_min, t_max = min(barres), max(barres)
        milieu = (t_min + t_max) // 2
        lignes.append(f"=== {sym} : du {datetime.fromtimestamp(t_min / 1000, timezone.utc):%Y-%m-%d} "
                      f"au {datetime.fromtimestamp(t_max / 1000, timezone.utc):%Y-%m-%d}")
        for seance, (fz, h, m, jours) in SEANCES.items():
            ouv = list(ouvertures(barres, fz, h, m, jours))
            for n_range in RANGES:
                for stop_mode in STOPS:
                    for objectif in OBJECTIFS:
                        for inverse in (False, True):
                            tr, refus = [], 0
                            for t in ouv:
                                x = trade(barres, sym, t, n_range, stop_mode, objectif, inverse)
                                if x is None:
                                    continue
                                if x.get("refuse"):
                                    refus += 1
                                    continue
                                tr.append(x)
                            rs = [x["r"] for x in tr]
                            s = stats(rs)
                            s1 = stats([x["r"] for x in tr if x["t"] < milieu])
                            s2 = stats([x["r"] for x in tr if x["t"] >= milieu])
                            cle = (f"{sym} | {seance} | range {n_range} min | stop {stop_mode} | "
                                   f"objectif {objectif or 'aucun'} | {'INVERSE' if inverse else 'cassure'}")
                            res[cle] = {"tout": s, "moitie1": s1, "moitie2": s2, "refuses": refus,
                                        "seances": len(ouv)}
        # lignes lisibles : cassure et inverse cote a cote
        for seance in SEANCES:
            lignes.append(f"--- {seance}")
            for n_range in RANGES:
                for stop_mode in STOPS:
                    for objectif in OBJECTIFS:
                        base = (f"{sym} | {seance} | range {n_range} min | stop {stop_mode} | "
                                f"objectif {objectif or 'aucun'} | ")
                        c, i = res[base + "cassure"], res[base + "INVERSE"]
                        s = c["tout"]
                        if not s["n"]:
                            lignes.append(f"range {n_range:2} stop {stop_mode:10} obj {str(objectif or '-'):4}: aucun trade")
                            continue
                        lignes.append(
                            f"range {n_range:2} stop {stop_mode:10} obj {str(objectif or '-'):4}: "
                            f"{s['n']:3} trades (refuses {c['refuses']:3}), esperance {s['esperance']:+.3f} R "
                            f"({s['esperance'] * DOLLARS_PAR_R:+.0f} $), total {s['total'] * DOLLARS_PAR_R:+7.0f} $, "
                            f"gagnants {s['gagnants_pct']:.0f} %, DD {s['dd']:.1f} R | moities "
                            f"{c['moitie1'].get('esperance', 0):+.2f} / {c['moitie2'].get('esperance', 0):+.2f} R"
                            f" | inverse {i['tout'].get('esperance', 0):+.3f} R")
    texte = "\n".join(lignes)
    with open(os.path.join(sortie, "cassure_ouverture.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    with open(os.path.join(sortie, "cassure_ouverture.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print(texte)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])
